<overview>
Hard limits, gotchas, and design trade-offs that constrain feature design. Check these BEFORE designing a feature to avoid hitting walls during implementation.
</overview>

<hard_limits>

## Hard Limits

| Constraint | Limit | Impact | Source |
|---|---|---|---|
| STS session policy packed size | **2048 bytes** | Cannot add per-resource Conditions for DynamoDB/Scheduler — use `Resource: "*"` and rely on execution role policy | AWS STS docs |
| Container max lifetime | **8 hours** (28800s) | Session forcefully terminated. Workspace saved on SIGTERM | cdk.json `session_max_lifetime` |
| Container idle timeout | **30 min** (1800s) | No message → container recycled. Next message triggers new container | cdk.json `session_idle_timeout` |
| SIGTERM grace period | **15 seconds** | Must save workspace within 10s (5s buffer) | AgentCore Runtime |
| Per-file workspace cap | **4096 chars** | Larger files truncated in system prompt injection | `sanitizeWorkspaceContent()` |
| Total workspace cap | **20,000 chars** | Lower-priority files skipped if total exceeds cap | `buildUserIdentityContext()` |
| S3 image upload max | **3.75 MB** | Bedrock multimodal limit per image | Bedrock ConverseStream |
| Image formats | JPEG, PNG, GIF, WebP | Only these formats accepted for multimodal | `VALID_BEDROCK_FORMATS` |
| API Gateway throttling | **Burst 50, sustained 100 req/s** | Rate limit across all channels | `router_stack.py` |
| AgentCore Runtime init | **120 seconds** | Container must respond to `/ping` within 120s or gets killed | AgentCore platform |
| OpenClaw resource names | `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` | Underscores not hyphens | AgentCore naming rules |
| CloudWatch PutMetricData | **1000 values per call, 150 TPS** | Token tracker batches at 60s intervals | CloudWatch API limits |
| DynamoDB namespace regex | `/^(telegram\|slack\|feishu\|dingtalk\|discord\|whatsapp\|web)_[a-zA-Z0-9_-]{1,64}$/` | New channels must be added to this regex. `web` added for Web UI | `scoped-credentials.js` |
| AgentCore WebSocket frame size | **32KB** | Platform caps WebSocket frames at 32KB. OpenClaw Gateway allows 25MB internally. Large `agents.files.set` or `chat.history` payloads need client-side chunking | AgentCore platform |
| Gateway Protocol methods | **97 methods, 19 events** | OpenClaw Gateway v3 full surface. Current `bridgeMessage()` only uses `connect` + `chat.send`. Expansion requires no container changes — platform auto-bridges to 18789 | OpenClaw Gateway Protocol v3 |

</hard_limits>

<design_gotchas>

## Design Gotchas (Learned the Hard Way)

### Deployment
- **`update-agent-runtime` is FULL REPLACE** — omitting `--environment-variables` wipes ALL env vars. Always include the complete env vars JSON. Most common deployment mistake
- **Runtime updates don't replace running containers** — must `stop-session` for changes to take effect
- **ECR repo naming**: Starter Toolkit uses `bedrock-agentcore-*` prefix — IAM policies must match

### Networking
- **Node.js 22 IPv6 issue** — Happy Eyeballs fails in VPCs without IPv6. `force-ipv4.js` patches DNS. Must use `NODE_OPTIONS=--dns-result-order=ipv4first`
- **Cross-region inference works through VPC endpoints** — `global.anthropic.claude-opus-4-6-v1` works fine through `bedrock-runtime` VPC endpoint
- **Security group egress TCP 443 only** — sufficient because DNS uses VPC resolver

### OpenClaw
- **Tool profile must be `"full"`** — do NOT use `"basic"` (undocumented, may disable web tools)
- **Sub-agent sandbox must be `"off"`** — no Docker inside AgentCore microVMs
- **`skipBootstrap` removed** — unknown config keys cause exit code 1
- **`skills.allowBundled` must be array** — `[]` for none, `["*"]` for all, NOT boolean
- **WebSocket origin enforcement** — must use `origin` option (not `headers.Origin`) in ws library + `allowedOrigins: ["*"]` in config
- **Workspace sync overwrites config** — `openclaw.json` excluded from sync via SKIP_PATTERNS

### Identity & Credentials
- **actorId vs namespace format** — actorId uses `:` (telegram:123), namespace uses `_` (telegram_123). Mix-up causes silent failures
- **Credential env var stripping** — OpenClaw spawned without `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_CONTAINER_CREDENTIALS_*`
- **Zero-access fallback** — if STS fails, OpenClaw starts with NO AWS access (tools fail gracefully, no cross-user access possible)

### CDK
- `logs.RetentionDays` is an enum — use helper in `stacks/__init__.py`
- Cross-stack cyclic deps — use string ARN params + `add_to_policy()` instead of `grant_*()`
- IAM role names are region-suffixed — `openclaw-agentcore-execution-role-{region}`

</design_gotchas>

<design_tradeoffs>

## Key Design Trade-offs

### STS Session Policy: Simplicity vs Granularity
**Choice**: Use `Resource: "*"` for DynamoDB/Scheduler in session policy, rely on execution role for resource-level restrictions.
**Why**: Per-resource Conditions (LeadingKeys, s3:prefix) quickly exceed 2048-byte packed limit.
**Risk**: If execution role is too broad, scoped credentials won't fully contain blast radius for non-S3 services.
**Mitigation**: Execution role has resource-level restrictions; session policy adds S3 namespace scoping.

### Lightweight Agent vs Full OpenClaw
**Choice**: Duplicate core tools in both lightweight-agent.js AND OpenClaw skills.
**Why**: ~2 min startup for OpenClaw is too slow for first response. Users need sub-10s response.
**Risk**: Feature drift between lightweight agent and OpenClaw implementations.
**Mitigation**: Keep lightweight agent tools minimal (CRUD-only). Complex features go to OpenClaw only.

### Single S3 Bucket for Everything
**Choice**: One bucket for user files, workspace sync, image uploads, and screenshots.
**Why**: Simpler IAM (one bucket in STS policy), one VPC endpoint, one lifecycle policy.
**Risk**: Namespace collision between system paths (_uploads/, .openclaw/) and user files.
**Mitigation**: System paths use underscore-prefixed directories (`_uploads/`, `_screenshots/`) and dot-prefixed (`.openclaw/`).

### Separate Cognito Pools
**Choice**: Two Cognito User Pools — bot identity (auto-provisioned, HMAC passwords) and admin (human login, MFA).
**Why**: Bot pool requirements (deterministic passwords, no email) are fundamentally incompatible with human admin login.
**Risk**: More infrastructure to manage.
**Mitigation**: Admin pool is small (few admins), bot pool is the high-volume one.

### In-Memory Token Tracking (Proxy) vs External Pipeline
**Choice**: Track tokens in proxy memory, batch-publish to CloudWatch every 60s.
**Why**: Zero latency, no external dependency, real-time data. Previous Bedrock Invocation Logging pipeline didn't work in AgentCore environment.
**Risk**: Token data lost on container crash (before CloudWatch publish).
**Mitigation**: 60s publish interval limits max data loss. SIGTERM triggers immediate flush.

</design_tradeoffs>
