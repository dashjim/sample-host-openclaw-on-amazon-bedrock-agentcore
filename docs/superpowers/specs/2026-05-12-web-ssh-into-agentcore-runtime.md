# 从 Web 浏览器 SSH 进入 AgentCore Runtime 容器

**日期**: 2026-05-12  
**状态**: Technical Reference  
**目标读者**: 需要从 Web Portal 远程访问 AgentCore Runtime 容器 Shell 的客户

---

## 1. 背景与需求

AgentCore Runtime 容器运行在 AWS 托管环境中，用户无法通过传统 SSH 直接访问。但在以下场景中需要交互式 Shell 访问：

- 调试 Agent 运行时问题（查看日志、环境变量、文件系统）
- 在 Runtime 中运行 CLI 工具（Claude Code、AWS CLI、自定义脚本）
- 实时观察 Agent 执行过程
- 开发时在线编辑代码

**解决方案**: 利用 AgentCore Runtime 的 **WebSocket 双向通信能力**，在容器内运行 Web Terminal (ttyd)，通过 SigV4 签名的 WSS URL 让浏览器直接连接容器内 Shell。

---

## 2. 架构概览

```
┌──────────────────────┐
│   Web Browser        │
│   (xterm.js)         │
│                      │
│  1. GET /connect     │
│     → 获取签名 URL   │
│                      │
│  2. new WebSocket()  │
│     → WSS 直连容器   │
└──────────┬───────────┘
           │ SigV4 签名的 WSS URL
           ▼
┌──────────────────────────────────────────────────────────────────┐
│   AWS AgentCore Platform                                         │
│                                                                  │
│   认证: SigV4QueryAuth (URL 中签名, 浏览器无需 AWS SDK)          │
│   路由: 根据 runtimeSessionId 路由到对应容器实例                  │
└──────────┬───────────────────────────────────────────────────────┘
           │ WebSocket Upgrade → /ws
           ▼
┌──────────────────────────────────────────────────────────────────┐
│   AgentCore Runtime Container (port 8080)                        │
│                                                                  │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │  contract-server (Node.js)                               │   │
│   │                                                          │   │
│   │  HTTP:                                                   │   │
│   │    GET  /ping         → 健康检查                         │   │
│   │    POST /invocations  → 初始化 + Session ID 注入         │   │
│   │                                                          │   │
│   │  WebSocket:                                              │   │
│   │    /ws → Bridge (双向中继) → ws://127.0.0.1:7681 [tty]  │   │
│   └──────────────────────────────────┬───────────────────────┘   │
│                                      │                           │
│   ┌──────────────────────────────────▼───────────────────────┐   │
│   │  ttyd (port 7681, 仅本地监听)                             │   │
│   │    → bash -l                                              │   │
│   │      → 用户在此执行任意命令 (SSH-like 体验)              │   │
│   └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. 连接流程

### 3.1 端到端时序

```
浏览器                     Web Backend                AgentCore 平台           容器
  │                            │                          │                    │
  │ 1. GET /sessions/{id}/connect                         │                    │
  │──────────────────────────▶│                           │                    │
  │                            │ 2. InvokeAgentRuntime    │                    │
  │                            │   (warmup action)        │                    │
  │                            │──────────────────────────▶│ ──────────────────▶│
  │                            │                           │  /invocations      │
  │                            │                           │  header 注入       │
  │                            │                           │  session ID        │
  │                            │                           │◀──────────────────│
  │                            │◀──────────────────────────│                    │
  │                            │                           │                    │
  │                            │ 3. SigV4QueryAuth 签名    │                    │
  │                            │    生成 WSS URL           │                    │
  │◀───────────────────────────│                           │                    │
  │  返回: wss://...?X-Amz-Signature=...                   │                    │
  │                            │                           │                    │
  │ 4. new WebSocket(signed_url)                           │                    │
  │────────────────────────────────────────────────────────▶│                    │
  │                            │                           │ 5. WS Upgrade /ws  │
  │                            │                           │──────────────────▶│
  │                            │                           │                    │
  │◀═══════════════════════════════════════════════════════════════════════════│
  │              双向 WebSocket 连接建立 (浏览器 ↔ 容器 ttyd)                  │
  │                            │                           │                    │
  │ 6. 终端输入/输出           │                           │                    │
  │◀══════════════════════════════════════════════════════════════════════════▶│
```

### 3.2 认证方式

AgentCore Runtime WebSocket 支持三种认证方式：

| 方式 | 适用场景 | 实现复杂度 |
|---|---|---|
| **SigV4QueryAuth (Pre-signed URL)** | Web 浏览器（无法设置自定义 header） | 中（后端签名） |
| SigV4 Header | 服务端/CLI 客户端 | 低（需 AWS SDK） |
| OAuth Bearer Token | 已有 JWT 的场景（Cognito） | 低 |

**Web 浏览器场景必须用 SigV4QueryAuth**，因为浏览器 WebSocket API 不支持自定义请求头。

---

## 4. 关键实现

### 4.1 Pre-signed WSS URL 生成（后端）

```python
from urllib.parse import urlencode, quote
import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest


def generate_presigned_wss_url(
    runtime_arn: str,
    runtime_session_id: str,
    region: str = "us-west-2",
    qualifier: str = "DEFAULT",
    expires: int = 300,  # 5 分钟有效期
) -> str:
    """生成 SigV4 签名的 WebSocket URL，浏览器可直接使用。"""

    encoded_arn = quote(runtime_arn, safe="")
    host = f"bedrock-agentcore.{region}.amazonaws.com"
    base_url = f"https://{host}/runtimes/{encoded_arn}/ws"

    params = {
        "qualifier": qualifier,
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": runtime_session_id,
    }
    url_with_params = f"{base_url}?{urlencode(params)}"

    sess = boto3.Session()
    creds = sess.get_credentials().get_frozen_credentials()

    request = AWSRequest(
        method="GET",
        url=url_with_params,
        headers={"host": host},
    )
    SigV4QueryAuth(creds, "bedrock-agentcore", region, expires=expires).add_auth(request)

    # 替换 https:// 为 wss://
    return request.url.replace("https://", "wss://")
```

生成的 URL 格式：
```
wss://bedrock-agentcore.us-west-2.amazonaws.com
  /runtimes/{encoded_arn}/ws
  ?qualifier=DEFAULT
  &X-Amzn-Bedrock-AgentCore-Runtime-Session-Id=ses_xxx
  &X-Amz-Algorithm=AWS4-HMAC-SHA256
  &X-Amz-Credential=AKIA.../20260512/us-west-2/bedrock-agentcore/aws4_request
  &X-Amz-Date=20260512T...
  &X-Amz-Expires=300
  &X-Amz-SignedHeaders=host
  &X-Amz-Signature=...
```

### 4.2 Container Contract Server（容器内 WebSocket Bridge）

容器需在 port 8080 同时处理 HTTP 健康检查和 WebSocket 连接。核心代码：

```javascript
const http = require("http");
const WebSocket = require("ws");

const PORT = 8080;
const TTYD_PORT = 7681;

// 启动 ttyd (Web terminal 后端)
const { spawn } = require("child_process");
spawn("ttyd", ["-p", String(TTYD_PORT), "-W", "bash", "-l"], {
  stdio: "inherit",
  env: { ...process.env },
});

// HTTP server: 健康检查 + WebSocket upgrade
const server = http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/ping") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "Healthy" }));
    return;
  }

  if (req.method === "POST" && req.url === "/invocations") {
    // 从 AgentCore 平台注入的 header 中提取 Session ID
    const sessionId = req.headers["x-amzn-bedrock-agentcore-runtime-session-id"];
    // 保存供后续使用...
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ status: "ready", sessionId }));
    });
    return;
  }

  res.writeHead(404);
  res.end("Not Found");
});

// WebSocket Bridge: 浏览器 ↔ ttyd
const wsBridgeServer = new WebSocket.Server({ noServer: true });

server.on("upgrade", (req, socket, head) => {
  if (req.url !== "/ws" && !req.url.startsWith("/ws?")) {
    socket.write("HTTP/1.1 404 Not Found\r\n\r\n");
    socket.destroy();
    return;
  }

  wsBridgeServer.handleUpgrade(req, socket, head, (downstream) => {
    // 连接内部 ttyd WebSocket (tty subprotocol)
    const upstream = new WebSocket(`ws://127.0.0.1:${TTYD_PORT}`, ["tty"]);

    let upstreamOpen = false;
    const pendingMessages = [];

    upstream.on("open", () => {
      upstreamOpen = true;
      for (const msg of pendingMessages) upstream.send(msg);
      pendingMessages.length = 0;
    });

    // 双向中继
    downstream.on("message", (data) => {
      if (upstreamOpen && upstream.readyState === WebSocket.OPEN) {
        upstream.send(data);
      } else {
        pendingMessages.push(data);
      }
    });

    upstream.on("message", (data) => {
      if (downstream.readyState === WebSocket.OPEN) downstream.send(data);
    });

    // 关闭传播
    downstream.on("close", () => {
      if (upstream.readyState === WebSocket.OPEN) upstream.close();
    });
    upstream.on("close", () => {
      if (downstream.readyState === WebSocket.OPEN) downstream.close();
    });
  });
});

server.listen(PORT);
```

### 4.3 前端 xterm.js 连接

```typescript
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";

async function connectToRuntime(sessionId: string) {
  // 1. 从后端获取签名 URL
  const resp = await fetch(`/api/sessions/${sessionId}/connect`);
  const { wss_url } = await resp.json();

  // 2. 初始化 xterm.js
  const terminal = new Terminal({ cursorBlink: true });
  const fitAddon = new FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open(document.getElementById("terminal")!);
  fitAddon.fit();

  // 3. 建立 WebSocket 连接
  const ws = new WebSocket(wss_url);

  ws.onopen = () => {
    console.log("Connected to AgentCore Runtime");
  };

  ws.onmessage = (event) => {
    // ttyd 协议: 第一个字节是消息类型
    // '0' = output, '1' = resize, ...
    if (event.data instanceof Blob) {
      event.data.arrayBuffer().then((buf) => {
        const data = new Uint8Array(buf);
        if (data[0] === 0x30) { // '0' = output
          terminal.write(data.slice(1));
        }
      });
    }
  };

  // 4. 用户输入 → WebSocket
  terminal.onData((data) => {
    // ttyd 协议: '0' + input data
    const payload = new Uint8Array(data.length + 1);
    payload[0] = 0x30; // '0' = input
    for (let i = 0; i < data.length; i++) {
      payload[i + 1] = data.charCodeAt(i);
    }
    ws.send(payload);
  });

  // 5. Keepalive (防止 Runtime idle timeout)
  setInterval(() => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(" "); // 空格 keepalive，contract-server 过滤不转发
    }
  }, 20000);

  // 6. 终端 resize
  terminal.onResize(({ cols, rows }) => {
    // ttyd 协议: '1' + JSON resize
    const msg = "1" + JSON.stringify({ columns: cols, rows: rows });
    if (ws.readyState === WebSocket.OPEN) ws.send(msg);
  });
}
```

### 4.4 Dockerfile（Runtime 容器镜像）

```dockerfile
FROM python:3.13-slim

# 安装 ttyd (Web terminal)
RUN apt-get update && apt-get install -y curl git build-essential wget jq \
    && wget -q https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.aarch64 \
       -O /usr/local/bin/ttyd \
    && chmod +x /usr/local/bin/ttyd \
    && rm -rf /var/lib/apt/lists/*

# 安装 Node.js (contract server)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Contract server
COPY contract-server/ /opt/contract-server/
RUN cd /opt/contract-server && npm install --production

# 启动脚本
COPY scripts/start.sh /opt/start.sh
RUN chmod +x /opt/start.sh

WORKDIR /workspace
EXPOSE 8080

CMD ["/opt/start.sh"]
```

### 4.5 AgentCore Runtime 配置

```yaml
# .bedrock_agentcore.yaml
runtime:
  name: my-web-terminal
  description: "Runtime with Web SSH access"
  protocol: HTTP
  network_mode: PUBLIC
  container:
    port: 8080
    health_check_path: /ping
```

---

## 5. 关键技术点

### 5.1 Session 路由机制

AgentCore 通过 `runtimeSessionId` 将 WebSocket 连接路由到正确的容器实例：

```
同一 runtimeSessionId → 同一容器实例
不同 runtimeSessionId → 可能不同容器实例
```

- Session ID 通过 URL query param 传入：`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`
- AgentCore 平台在 `/invocations` 调用时通过 HTTP header 注入：`x-amzn-bedrock-agentcore-runtime-session-id`
- 容器内通过读取此 header 确定自己的 session 身份

### 5.2 共用 8080 端口

AgentCore Runtime 协议要求容器监听 **单一端口** (8080)，同时提供：
- `GET /ping` — 健康检查（必须）
- `POST /invocations` — 标准 invoke（必须）
- `WebSocket /ws` — 双向通信（可选）

关键：HTTP server 和 WebSocket 共用同一个 `http.createServer`，通过 `server.on("upgrade")` 区分。

### 5.3 tty Subprotocol

ttyd 使用自定义的 WebSocket subprotocol `["tty"]`。Bridge 连接时必须声明：

```javascript
const upstream = new WebSocket("ws://127.0.0.1:7681", ["tty"]);
```

消息格式（二进制帧）：
- `0x30` ('0') + data = 标准 I/O
- `0x31` ('1') + JSON = 终端 resize
- `0x32` ('2') + data = 窗口标题

### 5.4 Keepalive 策略

AgentCore Runtime 有 idle timeout（默认约 10-15 分钟）。如果无活动，平台会终止容器。

解决方案：
- 前端每 20 秒发送空格文本帧 `" "`
- Contract server 检测到空格帧后**不转发给 ttyd**（避免在终端显示空格）
- 但会更新 `lastActivityTime`，对 `/ping` 报告 `HealthyBusy`

```javascript
downstream.on("message", (data) => {
  lastActivityTime = Math.floor(Date.now() / 1000);
  // 过滤 keepalive 帧
  if (typeof data === "string" && data === " ") return;
  // 正常消息转发
  if (upstream.readyState === WebSocket.OPEN) upstream.send(data);
});
```

### 5.5 Pre-signed URL 过期处理

SigV4QueryAuth URL 有时效（默认 5 分钟）。连接断开后需要重新获取：

```typescript
// 自动重连逻辑
ws.onclose = async () => {
  if (shouldReconnect && retries < 3) {
    retries++;
    const { wss_url } = await fetch(`/api/sessions/${id}/connect`).then(r => r.json());
    ws = new WebSocket(wss_url); // 新 URL，新签名
  }
};
```

### 5.6 Warmup（冷启动处理）

AgentCore Runtime 容器可能处于冷启动状态。连接前必须先 warmup：

```python
# 后端在返回 WSS URL 前先 warmup
def warmup_runtime(runtime_arn: str, session_id: str, region: str):
    client = boto3.client("bedrock-agentcore", region_name=region)
    client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        qualifier="DEFAULT",
        runtimeSessionId=session_id,
        payload=b'{"action": "warmup"}',
    )
```

Warmup 的作用：
1. 触发容器启动（如果尚未运行）
2. 通过 HTTP header 将 session ID 注入容器
3. 容器根据 session ID 恢复工作区（如有持久化）

---

## 6. Workspace 持久化（可选增强）

容器可能重启（Runtime 缩容/升级），需要持久化工作区文件。方案：S3 Sync。

```bash
# 容器启动后，等待 session ID 注入
while [ ! -f /tmp/.runtime-session-id ]; do sleep 1; done
SESSION_ID=$(cat /tmp/.runtime-session-id)

# 恢复上次工作区
S3_PATH="s3://${BUCKET}/workspaces/${SESSION_ID}"
aws s3 sync "${S3_PATH}/" /workspace/ --quiet

# 后台定期同步
while true; do
  sleep 30
  aws s3 sync /workspace/ "${S3_PATH}/" --quiet \
    --exclude "node_modules/*" --exclude ".venv/*"
done
```

---

## 7. 安全考虑

| 维度 | 措施 |
|---|---|
| **认证** | SigV4QueryAuth 签名 — 只有持有 AWS 凭证的后端能生成有效 URL |
| **时效** | URL 默认 5 分钟过期，即使泄露窗口有限 |
| **Session 隔离** | 每个 session ID 对应独立容器实例，用户间无法互访 |
| **网络** | 容器运行在 VPC 私有子网，仅通过 AgentCore 平台暴露 /ws |
| **最小权限** | 容器 IAM Role 仅授予业务所需权限，不能生成新 session URL |
| **审计** | AgentCore 平台记录所有 WebSocket 连接事件（CloudTrail） |

### 7.1 后端 IAM 权限（生成 URL 的服务）

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:InvokeAgentRuntime"
      ],
      "Resource": "arn:aws:bedrock-agentcore:*:*:runtime/*/endpoint/*"
    }
  ]
}
```

---

## 8. 替代认证方式

### 8.1 SigV4 Header（服务端/CLI）

适用于 Python/Node.js 客户端（可设置自定义 header）：

```python
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import websockets

def create_signed_headers(url, region="us-west-2"):
    session = boto3.Session()
    credentials = session.get_credentials()
    request = AWSRequest(method="GET", url=url, headers={"Host": urlparse(url).netloc})
    SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(request)
    return dict(request.headers)

# 连接
uri = f"wss://bedrock-agentcore.{region}.amazonaws.com/runtimes/{arn}/ws?qualifier=DEFAULT"
headers = create_signed_headers(uri)
headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] = session_id

async with websockets.connect(uri, additional_headers=headers) as ws:
    # 交互...
```

### 8.2 OAuth Bearer Token（Cognito JWT）

适用于已有 Cognito 登录态的场景（需 Runtime 配置 JWT Authorizer）：

```python
headers = {
    "Authorization": f"Bearer {cognito_access_token}",
    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
}

async with websockets.connect(uri, additional_headers=headers) as ws:
    # 交互...
```

---

## 9. WebSocket 端口被占用场景：反向隧道方案

### 9.1 问题描述

当容器内已有其他服务占据了 WebSocket 通道时（例如 OpenClaw Gateway 运行在 18789 端口，AgentCore 平台自动桥接到该端口），前面描述的 ttyd + Bridge 方案**不可用**——因为平台只桥接到一个内部 WS 端口，无法同时给终端用。

此时需要**反向隧道**方案：容器主动向外部运维网关发起出站连接，运维人员通过该网关访问容器内终端。

### 9.2 架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  AgentCore Runtime Container                                         │
│                                                                      │
│  ┌────────────────────┐  ┌──────────────────────┐                   │
│  │ OpenClaw Gateway   │  │ Contract Server      │                   │
│  │ port 18789         │  │ port 8080            │                   │
│  │ (平台自动桥接 WS)  │  │ (/ping, /invocations)│                   │
│  └────────────────────┘  └──────────────────────┘                   │
│                                                                      │
│  ┌────────────────────────────────────────────┐                     │
│  │ rtty client (或 rathole + ttyd)            │                     │
│  │                                            │                     │
│  │ 主动出站 WS 连接 ─────────────────────────────────────┐         │
│  └────────────────────────────────────────────┘           │         │
└───────────────────────────────────────────────────────────┼─────────┘
                                                            │
                                                            ▼ outbound WS
┌───────────────────────────────────────────────────────────────────────┐
│  运维网关 (rttys / rathole server / ShellHub)                         │
│                                                                       │
│  - 接收容器主动注册                                                    │
│  - 提供 Web Terminal UI (xterm.js)                                    │
│  - mTLS / Token 认证                                                  │
│  - 管理员浏览器访问 → 获得容器 Shell                                  │
└───────────────────────────────────────────────────────────────────────┘
          ▲
          │ HTTPS
┌─────────┴─────────┐
│  管理员浏览器      │
│  Web Terminal      │
└───────────────────┘
```

### 9.3 推荐开源方案

#### 方案 A: rtty（一体化，最推荐）

| 项目 | 详情 |
|---|---|
| **GitHub (Go 客户端)** | https://github.com/zhaojh329/rtty-go (推荐，有预编译 ARM64) |
| **GitHub (服务器)** | https://github.com/zhaojh329/rttys |
| **架构** | Go 客户端 (容器内) → 出站 WS → Go 服务器 (rttys) ← 管理员浏览器 |
| **Web UI** | 内置 xterm.js 终端，支持窗口分割 |
| **安全** | mTLS 双向证书认证 |
| **特性** | 文件上传下载、批量命令、HTTP 代理 |
| **客户端大小** | ~2.4MB (Go，静态链接无外部依赖) |
| **ARM64 支持** | ✅ 预编译 `linux-arm64` 二进制可直接使用 |

> **注意：AgentCore Runtime 使用 ARM64 (aarch64) 架构。** 必须使用 Go 版本客户端 (`rtty-go`)，它提供预编译的 `linux-arm64` 二进制。原始 C 版本 (`rtty`) 不提供 ARM64 预编译包，需要自行交叉编译。

**容器内启动：**

```bash
# rtty-go 客户端主动连接到运维网关
rtty -I "runtime-${SESSION_ID}" \
     -h ops-gateway.company.com \
     -p 5912 \
     -a \              # 自动重连
     -s               # SSL
```

**rttys 服务器部署（运维侧）：**

```bash
# 运维网关，管理员通过浏览器访问
rttys -cert server.crt -key server.key -addr :5912 -http-port 5913
# 管理员打开 https://ops-gateway.company.com:5913
# 选择 device ID → 获得 Web Terminal
```

#### 方案 B: ttyd + rathole（组合方案，更灵活）

| 组件 | 作用 | GitHub |
|---|---|---|
| **ttyd** | 容器内 Web Terminal (127.0.0.1:7681) | https://github.com/tsl0922/ttyd (11.6k stars, MIT) |
| **rathole** 客户端 | 容器内，主动出站建立反向隧道 | https://github.com/rathole-org/rathole (13.5k stars, Apache-2.0, Rust) |
| **rathole** 服务器 | 运维侧，接收隧道并暴露端口 | 同上 |

**容器内配置 (`rathole-client.toml`)：**

```toml
[client]
remote_addr = "ops-gateway.company.com:2333"

[client.transport]
type = "tls"

[client.services.terminal]
local_addr = "127.0.0.1:7681"   # ttyd 本地端口
```

**服务器端配置 (`rathole-server.toml`)：**

```toml
[server]
bind_addr = "0.0.0.0:2333"

[server.transport]
type = "tls"

[server.services.terminal]
bind_addr = "0.0.0.0:7681"      # 管理员浏览器访问此端口
token = "secret-token-here"
```

**管理员访问：** 浏览器打开 `https://ops-gateway.company.com:7681` → 直接看到 ttyd Web Terminal。

#### 方案 C: ShellHub（企业级，含审计）

| 项目 | 详情 |
|---|---|
| **GitHub** | https://github.com/shellhub-io/shellhub (2k+ stars, Apache-2.0) |
| **架构** | Go Agent (容器内) → 出站连接 → ShellHub Gateway ← 管理员 Web UI |
| **Web UI** | 内置，含会话录像回放 |
| **特性** | RBAC、审计日志、防火墙规则、SCP/SFTP、多设备管理 |
| **适合** | 需要合规审计、多容器管理的企业场景 |

### 9.4 Dockerfile 改动示例（以 rtty-go 为例）

```dockerfile
# 在现有 Dockerfile 基础上添加 rtty-go 客户端 (ARM64)
RUN wget -q https://github.com/zhaojh329/rtty-go/releases/download/v1.1.1/rtty-1.1.1-linux-arm64.tar.bz2 \
    && tar -xjf rtty-1.1.1-linux-arm64.tar.bz2 \
    && mv rtty /usr/local/bin/rtty \
    && rm -f rtty-1.1.1-linux-arm64.tar.bz2 \
    && chmod +x /usr/local/bin/rtty
```

**启动脚本添加：**

```bash
# 在 start.sh 中，OpenClaw 启动之后添加
if [ -n "$OPS_GATEWAY_HOST" ]; then
  rtty -I "runtime-$(cat /tmp/.runtime-session-id 2>/dev/null || echo unknown)" \
       -h "$OPS_GATEWAY_HOST" \
       -p "${OPS_GATEWAY_PORT:-5912}" \
       -a -s &
  echo "[start.sh] rtty reverse tunnel started → ${OPS_GATEWAY_HOST}"
fi
```

### 9.5 安全考虑

#### 9.5.1 基础安全措施

| 维度 | 措施 |
|---|---|
| **认证** | mTLS 证书（rtty）或 Token（rathole）认证，防止未授权容器注册 |
| **网络** | 容器仅出站连接，不开放入站端口 |
| **隔离** | 每个容器用唯一 ID 注册（session ID），运维人员选择目标容器 |
| **审计** | ShellHub 提供完整会话录像；rtty/rathole 可配合日志记录 |
| **按需启用** | 通过环境变量 `OPS_GATEWAY_HOST` 控制，不设置则不启动隧道 |
| **最小权限** | 运维网关仅授权特定管理员访问 |

#### 9.5.2 核心风险：Agent 是否会通过隧道泄露数据？

**风险场景：** 容器内 AI Agent 具备 bash/exec 执行能力（如 OpenClaw 的 exec 工具）。如果 Agent 被 prompt injection 攻击，或恶意用户诱导 Agent 执行命令，Agent 可能：

1. 通过 rtty 隧道向运维网关发送敏感数据
2. 利用容器内的出站网络将数据外泄到任意地址
3. 读取容器内其他进程的凭证或环境变量

**分析：rtty 隧道本身不增加额外的数据泄露面。**

| 威胁 | 无 rtty 时 | 有 rtty 时 | 结论 |
|---|---|---|---|
| Agent 通过网络外泄 | ✅ 已有风险（curl/wget 外发） | 风险不变 | rtty 不是新增攻击面 |
| Agent 读取凭证 | ✅ 已有风险（cat /proc/*/environ） | 风险不变 | rtty 不影响 |
| Agent 向 rtty 会话注入输出 | ❌ 不存在 | ⚠️ 新风险（理论上） | 需缓解 |
| 外部攻击者通过隧道入侵 | ❌ | ⚠️ 新风险 | 需认证保护 |

**关键结论：** Agent 的数据外泄能力来自网络出站权限（curl、DNS 隧道等），而非 rtty 隧道。rtty 隧道新增的风险主要是**运维通道被滥用**，而非 Agent 主动利用。

#### 9.5.3 缓解措施

**1. 进程隔离 — Agent 无法直接操作 rtty 连接：**

```bash
# rtty 以独立进程运行，Agent 的 exec 工具不能直接写入 rtty 的 WS 连接
# Agent 能做的最多是 kill rtty 进程（DoS），但无法注入数据到管理员终端

# 加固：用独立用户运行 rtty
useradd -r -s /bin/false rtty-user
su -s /bin/sh rtty-user -c "rtty -I runtime-xxx -h gateway -p 5912 -a -s"
```

**2. 网络层限制 — 限制出站目标：**

```
AgentCore Runtime 网络策略:

方案 A: VPC Security Group 限制出站
  - 仅允许出站到运维网关 IP:PORT (rtty)
  - 仅允许出站到已知服务 (Bedrock, S3, etc.)
  - 拒绝其他出站 → Agent 无法 curl 到任意地址

方案 B: 使用 SANDBOX 网络模式（如适用）
  - 无公网出站，仅 S3 + DNS
  - 但此模式下 rtty 隧道也无法出站 → 互斥

方案 C: VPC + 精细 Security Group（推荐）
  - 出站规则白名单：运维网关 IP + AWS 服务 endpoint
  - Agent 的 curl/wget 只能到达白名单地址
```

**3. rtty 服务端安全加固：**

```
运维网关侧:
  - mTLS: 容器必须持有客户端证书才能注册
  - Token 轮换: 每个容器使用唯一 token，定期轮换
  - IP 白名单: 仅允许来自 AgentCore VPC NAT IP 的连接
  - 会话录像: 记录所有终端操作（事后审计）
  - RBAC: 不同管理员只能访问授权的容器
```

**4. 与 Agent exec 权限的关系：**

| Agent 配置 | 风险等级 | 说明 |
|---|---|---|
| exec 工具被 deny | 低 | Agent 无法执行任意命令，rtty 仅供管理员使用 |
| exec 工具被 allow + scoped credentials | 中 | Agent 可执行命令但凭证受限，无法读取 rtty 证书 |
| exec 工具被 allow + 无 credential scoping | 高 | Agent 可能读取任何文件，需额外加固 |

**5. 推荐安全架构：**

```
┌─────────────────────────────────────────────────────────┐
│  Container                                               │
│                                                          │
│  ┌──────────────────────┐  ┌─────────────────────────┐  │
│  │ Agent 进程            │  │ rtty 进程               │  │
│  │ (scoped credentials) │  │ (独立用户, 独立凭证)    │  │
│  │                      │  │                         │  │
│  │ - exec 受限          │  │ - 仅连接运维网关        │  │
│  │ - S3 namespace 隔离  │  │ - 管理员交互式使用      │  │
│  │ - 无法读取 rtty cert │  │ - Agent 不可控制        │  │
│  └──────────────────────┘  └─────────────────────────┘  │
│                                                          │
│  Security Group: 出站仅允许 AWS endpoints + 运维网关 IP  │
└─────────────────────────────────────────────────────────┘
```

**总结：** rtty 隧道的安全风险**不在于 Agent 会通过它泄露数据**（Agent 有更直接的方式如 curl），而在于**运维通道本身需要保护**（认证、授权、审计）。正确的做法是：
1. Agent 的 exec 权限通过工具 deny list 和 scoped credentials 控制（已有机制）
2. rtty 运行在独立进程/用户下，Agent 无法操控
3. 网络出站通过 Security Group 白名单限制（无论有没有 rtty 都应该做）
4. 运维网关用 mTLS + RBAC 保护，确保只有授权管理员可以使用

### 9.6 连接稳定性与断开处理

反向隧道是一条长连接，以下情况会导致断开：

| 断开原因 | 触发条件 | 恢复方式 |
|---|---|---|
| **AgentCore idle timeout** | 无 invoke 活动超过 `session_idle_timeout`（默认 15-30 分钟） | 容器被杀，下次 warmup 后隧道自动重连 |
| **AgentCore max lifetime** | 容器运行超过 `session_max_lifetime`（默认 8 小时） | 同上 |
| **网络抖动** | NAT 超时、VPC endpoint 重置 | rtty `-a` 参数自动重连（秒级恢复） |
| **运维网关重启** | 服务端维护 | rtty 自动重连到新实例 |

**处理策略：**

```
容器启动 → rtty 自动连接 → 管理员可访问 (online)
                                ↓ (网络抖动)
rtty 检测断开 → 自动重试 → 恢复连接 (几秒内)
                                ↓ (idle timeout 触发)
容器被杀 → rtty 进程终止 → 管理员看到 offline
                                ↓ (管理员需要访问)
warmup 容器 → 容器重启 → rtty 随容器启动 → 自动连接 → online
```

**关键设计决策：**

1. **rtty/rathole 都支持自动重连** — 网络层断开秒级恢复，无需人工干预
2. **容器生命周期由 AgentCore 管理** — 不要试图阻止 idle timeout（浪费资源），接受容器有生命周期
3. **按需访问模式** — 管理员需要终端时先确保容器存活（发一次 warmup invoke），隧道自动恢复
4. **运维网关显示设备状态** — rttys/ShellHub 界面实时显示容器 online/offline，管理员一目了然

**保活方案（如需要持续在线）：**

如果业务要求容器不被回收（长期保持终端可用），可通过定时 invoke 维持活跃：

```bash
# 外部定时任务（如 EventBridge），每 10 分钟 warmup 一次
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn $RUNTIME_ARN \
  --runtime-session-id $SESSION_ID \
  --payload '{"action":"warmup"}' \
  --qualifier DEFAULT
```

但这会产生持续的 Runtime 计费成本，仅在调试期间推荐使用。

### 9.7 方案对比

| | rtty | ttyd + rathole | ShellHub | Teleport |
|---|---|---|---|---|
| **一体化** | ✅ 客户端+服务器+Web UI | 需组合 | ✅ | ✅ |
| **客户端大小** | ~100KB (C) | ~500KB+5MB | ~10MB (Go) | ~50MB |
| **Web Terminal** | 内置 | ttyd 提供 | 内置 | 内置 |
| **审计录像** | ❌ | ❌ | ✅ | ✅ |
| **RBAC** | 基础 | ❌ | ✅ | ✅ |
| **复杂度** | 低 | 中 | 中 | 高 |
| **适合场景** | 开发调试 | 灵活定制 | 企业运维 | 大规模基础设施 |
| **License** | MIT | Apache-2.0 | Apache-2.0 | AGPL-3.0 |

### 9.8 适用场景决策树

```
WebSocket 端口是否被其他服务占用？（如 OpenClaw Gateway）
  │
  ├── 否 → 使用标准方案（第 1-8 章：ttyd + Bridge + SigV4 Pre-signed URL）
  │
  └── 是 → 需要反向隧道
        │
        ├── 只需偶尔调试？ → rtty（最轻量，按需启动）
        ├── 需要灵活组合？ → ttyd + rathole/chisel
        └── 需要审计合规？ → ShellHub 或 Teleport
```

---

## 10. 完整方案对比

| 方案 | 描述 | 优势 | 限制 |
|---|---|---|---|
| **Web Terminal (ttyd + Bridge)** | 容器内 ttyd + WebSocket bridge | 完整 Shell 体验，零客户端安装 | 需容器占有 WebSocket 通道 |
| **反向隧道 (rtty / rathole)** | 容器主动出站连接运维网关 | 不占 WS 端口，和其他服务共存 | 需部署外部运维网关 |
| **AgentCore Bi-directional WS** | 纯 WebSocket 协议自定义 | 轻量，可自定义协议 | 需自行实现终端仿真 |
| **ECS Exec** | 通过 ECS Agent 连入 | AWS 原生，无需容器改造 | 仅 ECS 部署可用，AgentCore 托管不适用 |
| **SSM Session Manager** | 通过 Systems Manager | 审计完善 | 需安装 SSM Agent，AgentCore 不支持 |

**推荐方案**:
- **WebSocket 端口可用** → ttyd + Bridge（第 1-8 章）
- **WebSocket 端口被占用** → rtty 反向隧道（第 9 章）

---

## 11. Reference

### 11.1 项目内参考实现

| 参考 | 位置 |
|---|---|
| **完整 Contract Server 实现** | `agentcore-demo-portal/meta-builder-runtime/contract-server/main.js` |
| **Dockerfile** | `agentcore-demo-portal/meta-builder-runtime/Dockerfile` |
| **启动脚本 (ttyd + S3 sync)** | `agentcore-demo-portal/meta-builder-runtime/scripts/start.sh` |
| **Pre-signed URL 生成** | `agentcore-demo-portal/backend/app/services/builder_service.py` |
| **Runtime 配置** | `agentcore-demo-portal/meta-builder-runtime/.bedrock_agentcore.yaml` |
| **Phase 2 完整设计文档** | `agentcore-demo-portal/docs/2026-04-27-demo-portal-phase2-design.md` |

### 11.2 AgentCore Samples (GitHub)

| 参考 | 链接 |
|---|---|
| **WebSocket Helper (SigV4 签名工具)** | [06-bi-directional-streaming/utils/websocket_helpers.py](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/01-tutorials/01-AgentCore-runtime/06-bi-directional-streaming/utils/websocket_helpers.py) |
| **Strands WS Server (WebSocket Runtime 示例)** | [06-bi-directional-streaming/02-strands-ws/websocket/server.py](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/01-tutorials/01-AgentCore-runtime/06-bi-directional-streaming/02-strands-ws/websocket/server.py) |
| **Strands WS Client (Pre-signed URL + Web UI)** | [06-bi-directional-streaming/02-strands-ws/client/client.py](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/01-tutorials/01-AgentCore-runtime/06-bi-directional-streaming/02-strands-ws/client/client.py) |
| **Deploy 工具 (Runtime 部署)** | [06-bi-directional-streaming/utils/deploy.py](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/01-tutorials/01-AgentCore-runtime/06-bi-directional-streaming/utils/deploy.py) |
| **SigV4 Streamable HTTP (MCP over HTTP + SigV4)** | [01-AgentCore-runtime/02-hosting-MCP-server/streamable_http_sigv4.py](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/01-tutorials/01-AgentCore-runtime/02-hosting-MCP-server/streamable_http_sigv4.py) |
| **AgentCore Runtime Tutorials** | [01-tutorials/01-AgentCore-runtime/](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/01-AgentCore-runtime) |

### 11.3 外部参考

| 参考 | 链接 |
|---|---|
| **OpenClaw WebSocket Sample (验证过的 Bridge 模式)** | [github.com/dashjim/sample-host-openclaw-on-amazon-bedrock-agentcore](https://github.com/dashjim/sample-host-openclaw-on-amazon-bedrock-agentcore/commit/b71d125) |
| **ttyd (Web Terminal)** | [github.com/tsl0922/ttyd](https://github.com/tsl0922/ttyd) |
| **xterm.js (前端终端组件)** | [github.com/xtermjs/xterm.js](https://github.com/xtermjs/xterm.js) |
| **AgentCore Runtime 文档** | https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore-runtime.html |
| **SigV4 签名文档** | https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html |

---

### 11.4 反向隧道方案

| 参考 | 链接 |
|---|---|
| **rtty (反向 WS + Web Terminal 一体化)** | https://github.com/zhaojh329/rtty |
| **rathole (Rust 反向隧道，轻量高性能)** | https://github.com/rathole-org/rathole |
| **chisel (HTTP 反向隧道，单二进制)** | https://github.com/jpillora/chisel |
| **ShellHub (企业级远程访问 + 审计)** | https://github.com/shellhub-io/shellhub |
| **Teleport (零信任基础设施访问)** | https://github.com/gravitational/teleport |
| **frp (通用反向代理)** | https://github.com/fatedier/frp |

---

## 12. 总结

**从 Web 浏览器 "SSH" 进入 AgentCore Runtime 容器的实现路径：**

1. **容器侧**: 安装 `ttyd` + Node.js `contract-server`，共用 8080 端口提供 HTTP 健康检查 + WebSocket Bridge
2. **后端侧**: 使用 `SigV4QueryAuth` 生成 pre-signed WSS URL，浏览器无需 AWS SDK 即可连接
3. **前端侧**: 使用 `xterm.js` 渲染终端，通过 WebSocket 与容器双向通信
4. **平台侧**: AgentCore 负责认证 (SigV4)、Session 路由、容器生命周期管理

整个方案无需修改 AgentCore 平台，完全利用现有的 HTTP + WebSocket 协议能力实现。
