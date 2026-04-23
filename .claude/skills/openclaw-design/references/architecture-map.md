<overview>
Maps the project's architecture layers, component boundaries, and ownership. Use this to understand where a change belongs before implementing it.
</overview>

<layers>

## System Layers (outside-in)

```
Layer 1: Channel Ingestion
  API Gateway HTTP API → Router Lambda → DynamoDB identity
  Owner: CDK (router_stack.py, security_stack.py)
  Code: lambda/router/index.py

Layer 1.5: Browser WebSocket (Web UI channel)
  Browser → wss://bedrock-agentcore.<region>.amazonaws.com/runtimes/<arn>/ws
  Auth: SigV4 presigned URL or OAuth Bearer (base64UrlBearerAuthorization subprotocol)
  Session routing: X-Amzn-Bedrock-AgentCore-Runtime-Session-Id header
  Platform auto-discovers container WS listener (OpenClaw Gateway on 18789)
  and bridges directly — no /ws handler needed on 8080 (POC verified Apr 2026)
  Requires HTTP bootstrap (warmup via invoke_agent_runtime) before WS connect
  Owner: CDK (web_ui_stack.py — Cognito, API Lambda, S3+CloudFront)

Layer 2: AgentCore Runtime (per-user microVM)
  Contract Server (8080) → Proxy (18790) → OpenClaw Gateway (18789)
  OpenClaw Gateway: full Gateway Protocol v3 (97 methods, 19 events)
  Current usage: bridgeMessage() uses only connect + chat.send
  Browser WS: platform auto-bridges to 18789 (no 8080 /ws handler)
  Owner: Starter Toolkit (runtime + ECR image), CDK (IAM, SG, S3)
  Code: bridge/

Layer 3: AI Model
  Bedrock ConverseStream API (via proxy)
  Owner: AWS managed
  Config: BEDROCK_MODEL_ID env var, cdk.json default_model_id

Layer 4: Persistence
  DynamoDB (identity, sessions, cron), S3 (user files, workspace, images)
  Owner: CDK (router_stack.py, agentcore_stack.py)

Layer 5: Scheduling
  EventBridge Scheduler → Cron Lambda → AgentCore
  Owner: CDK (cron_stack.py)
  Code: lambda/cron/index.py

Layer 6: Admin Control Plane
  CloudFront+S3 (React SPA) → API Gateway → Admin Lambda
  Owner: CDK (admin_stack.py)
  Code: admin-ui/, lambda/admin/

Layer 7: Observability
  CloudWatch dashboards, alarms, Bedrock logging, token tracking
  Owner: CDK (observability_stack.py, token_monitoring_stack.py)
  Code: lambda/token_metrics/
```
</layers>

<ownership_boundaries>

## CDK vs Starter Toolkit Boundary

**CDK manages** (8 stacks in `stacks/`):
- VPC, subnets, NAT, VPC endpoints (`vpc_stack.py`)
- KMS, Secrets Manager, Cognito (`security_stack.py`)
- IAM execution role, security group, S3 bucket (`agentcore_stack.py`)
- Router Lambda, API Gateway, DynamoDB identity table (`router_stack.py`)
- Cron Lambda, EventBridge Scheduler (`cron_stack.py`)
- Dashboards, alarms (`observability_stack.py`, `token_monitoring_stack.py`)
- Admin Cognito, API Gateway, Lambda, CloudFront (`admin_stack.py`)

**Starter Toolkit manages** (via `agentcore` CLI or direct API):
- AgentCore Runtime resource
- AgentCore Runtime Endpoint
- ECR repository + Docker image builds (CodeBuild or local)
- Container environment variables (via `update-agent-runtime`)

**Key implication**: Container env var changes require `update-agent-runtime` API call (full replace!) AND `stop-session` to take effect on running containers. CDK deploy alone is NOT sufficient for container changes.
</ownership_boundaries>

<container_internals>

## Container Component Map

```
agentcore-contract.js (port 8080)
  ├── /ping → health check (responds immediately, no init)
  ├── /invocations → lazy init on first chat/warmup
  │     ├── Fetch secrets (Secrets Manager)
  │     ├── STS AssumeRole (scoped credentials)
  │     ├── Start proxy (18790) with USER_ID, CHANNEL env
  │     ├── Start OpenClaw (18789) with scoped creds
  │     ├── Restore .openclaw/ from S3 (background)
  │     ├── Start browser session (if BROWSER_IDENTIFIER set)
  │     └── Credential refresh timer (45 min)
  ├── Routing: lightweight-agent (warm-up) → WebSocket bridge (full)
  └── SIGTERM: save workspace, stop browser, kill children

lightweight-agent.js
  ├── Agentic loop: proxy → Bedrock ConverseStream
  ├── 17 tools: s3-user-files (4), eventbridge-cron (4),
  │   clawhub-manage (3), api-keys (4), web_fetch, web_search
  └── Appends warm-up footer to responses

agentcore-proxy.js (port 18790)
  ├── OpenAI-compatible API → Bedrock ConverseStream
  ├── Cognito identity (auto-provision, JWT cache)
  ├── User identity injection into system prompt
  ├── Multimodal image handling (S3 fetch → Bedrock content blocks)
  ├── Subagent model routing (detects bedrock-agentcore-subagent)
  ├── Token tracking → CloudWatch custom metrics
  └── Per-user workspace file pre-loading

workspace-sync.js
  ├── Restore .openclaw/ from S3 on init
  ├── Periodic save (every 5 min)
  └── Final save on SIGTERM (10s grace)

scoped-credentials.js
  ├── STS AssumeRole with session policy
  ├── Write credential_process files to /tmp/scoped-creds/
  └── Refresh timer (45 min interval)
```
</container_internals>

<cross_stack_dependencies>

## Stack Dependency Graph

```
OpenClawVpc ─────────────┐
                         ├──→ OpenClawAgentCore ──→ OpenClawRouter ──→ OpenClawCron
OpenClawSecurity ────────┘         │                      │
                                   │                      │
                                   └──────→ OpenClawAdmin ┘
                                   
OpenClawObservability (independent) ──→ OpenClawTokenMonitoring
```

**Deploy order matters**: Phase 1 (Vpc, Security, AgentCore, Observability) → Phase 2 (Starter Toolkit) → Phase 3 (Router, Cron, TokenMonitoring, Admin)

Router and Cron stacks depend on runtime_id from Starter Toolkit output (stored in cdk.json).
</cross_stack_dependencies>
