<overview>
Recurring design patterns in this project. When adding a feature, check if it fits an existing pattern before inventing a new one.
</overview>

<pattern name="channel-integration">

## Channel Integration Pattern

Every messaging channel follows this exact flow:

```
Channel webhook POST → API Gateway route → Router Lambda
  1. Validate webhook signature (channel-specific)
  2. Return 200 immediately (prevent webhook timeout)
  3. Self-invoke Lambda async (InvocationType=Event)
  4. Parse message (text, images, channel-specific fields)
  5. Resolve user identity (DynamoDB: CHANNEL# → USER#)
  6. Get/create AgentCore session (deterministic session ID)
  7. Invoke AgentCore Runtime (per-user session)
  8. Send response back via channel API
```

**Per-channel touchpoints** (what varies):
| Component | File | What changes |
|---|---|---|
| Secret | `security_stack.py` | `openclaw/channels/{channel}` |
| Route | `router_stack.py` | `POST /webhook/{channel}` |
| Handler | `lambda/router/index.py` | Validation, parsing, sending |
| Cron delivery | `lambda/cron/index.py` | `send_{channel}_message()` |
| Setup script | `scripts/setup-{channel}.sh` | Interactive setup |
| Tests | `lambda/router/test_{channel}.py` | Channel-specific tests |

**Reference implementations**: Feishu (most complex — AES encryption, token refresh, group/P2P distinction) and Telegram (simplest — header token validation).
</pattern>

<pattern name="per-user-isolation">

## Per-User Isolation Pattern

Three-layer defense-in-depth for any per-user resource:

```
Layer 1: IAM Execution Role (broad — container base capabilities)
Layer 2: STS Session Policy (hard limit — namespace-scoped)
Layer 3: Application-level namespace validation (defense-in-depth)
```

**When adding new per-user data:**
1. Ensure the resource is under the user's namespace prefix
2. Verify STS session policy allows access (check `scoped-credentials.js`)
3. Add application-level namespace validation in the skill/tool code
4. Watch the STS policy size (2048-byte packed limit — add services sparingly)

**S3 key format**: `{namespace}/{path}` where `namespace = actorId.replace(/:/g, "_")`
**DynamoDB**: Use `PK = USER#{internalUserId}` for user-scoped records

**Critical**: The STS session policy is the hard security boundary. Application-level checks are defense-in-depth only. Never rely solely on application code for isolation.
</pattern>

<pattern name="two-phase-startup">

## Two-Phase Startup Pattern

```
Phase 1 (Warm-up): lightweight-agent.js
  - Available: ~5s after cold start
  - Handles: chat messages via agentic loop (proxy → Bedrock)
  - Tools: 17 built-in tools (s3-user-files, cron, clawhub, api-keys, web_fetch, web_search)
  - Indicator: response includes "_Warm-up mode_" footer
  - Use for: tools that must be available immediately

Phase 2 (Full): OpenClaw Gateway via WebSocket bridge
  - Available: ~1-2 min after cold start
  - Handles: all messages via OpenClaw runtime
  - Tools: full profile + ClawHub skills + sub-agents + custom skills
  - Gateway Protocol v3: 97 methods, 19 events (currently only connect + chat.send used)
  - Browser WS: platform auto-bridges to container port 18789 (no /ws handler on 8080)
  - Indicator: no warm-up footer
  - Use for: complex features needing OpenClaw ecosystem
```

**Design decision for new features:**
- If the tool must work during warm-up → add to `lightweight-agent.js` TOOLS array
- If the tool only works after full startup → create OpenClaw skill in `bridge/skills/`
- If both → implement in lightweight-agent AND as OpenClaw skill (api-keys does this)
</pattern>

<pattern name="async-dispatch">

## Async Self-Invoke Pattern

Used by Router Lambda to avoid webhook timeouts:

```python
# Sync path (returns immediately)
if not is_async:
    validate_webhook(headers, body)
    _self_invoke_async(channel, body, headers)
    return {"statusCode": 200}

# Async path (does real work)
if is_async:
    handle_{channel}(body, headers)
```

**Why**: Channel webhooks have strict timeout requirements (Telegram ~60s, Slack ~3s for challenge). AgentCore cold start can take 30-60s. Returning 200 immediately prevents retry storms.

**Pattern**: Lambda self-invokes with `InvocationType='Event'` and adds `__async_channel` marker to distinguish sync vs async invocation.
</pattern>

<pattern name="credential-scoping">

## Credential Scoping Pattern

OpenClaw processes run with restricted credentials. The proxy keeps full credentials.

```
Container credentials (full execution role)
  │
  ├── Proxy (trusted code) — keeps full credentials
  │     Uses for: Bedrock, Cognito, S3 image access
  │
  └── STS AssumeRole + session policy → scoped credentials
        Written to /tmp/scoped-creds/ as credential_process files
        │
        └── OpenClaw (untrusted — LLM-driven) — uses scoped credentials
              Can only access: S3 {namespace}/*, Secrets Manager openclaw/user/{ns}/*
              Cannot access: container credentials, other users' data
```

**Credential files**: AWS config + credential_process format. OpenClaw uses `AWS_CONFIG_FILE` + `AWS_SDK_LOAD_CONFIG=1`. Container credential env vars are explicitly stripped from OpenClaw's environment.

**Refresh cycle**: 45-minute interval (STS max duration is 1 hour).
</pattern>

<pattern name="workspace-persistence">

## Workspace Persistence Pattern

```
Session start → restore .openclaw/ from S3
  Every 5 min → periodic save to S3
  SIGTERM → final save (10s grace period)
Session end → workspace persisted in S3
Next session → restore from S3 (seamless continuity)
```

**Skip patterns**: `node_modules/`, `.cache/`, `*.log`, files > 10MB
**S3 prefix**: `{namespace}/.openclaw/`
**Config exclusion**: `openclaw.json` excluded from sync — always generated by `writeOpenClawConfig()`
</pattern>

<pattern name="identity-resolution">

## Identity Resolution Pattern

```
External identity: telegram:123456789 (actorId)
  ↓ Router Lambda
Internal identity: user_abc123 (deterministic SHA256-based)
  ↓ DynamoDB lookup/create
Session identity: ses_user_abc123_hash (deterministic)
  ↓ AgentCore per-user session
Namespace: telegram_123456789 (S3/resource prefix)
```

**Cross-channel binding**: User generates 6-char bind code on channel A → enters code on channel B → both channels map to same internal user ID → same session, same workspace, same files.

**Priority order for identity in proxy**: `USER_ID` env var → `x-openclaw-actor-id` header → OpenAI `user` field → message parsing → fallback `default-user`
</pattern>

<pattern name="browser-websocket-connection">

## Browser WebSocket Connection Pattern (POC verified Apr 2026)

```
Phase 1: HTTP Bootstrap (reuse existing channel logic)
  Browser → POST /api/session (Cognito JWT) → Web API Lambda
    → resolve_user("web", cognito_sub) → DynamoDB
    → get_or_create_session(user_id)
    → invoke_agent_runtime(action: "warmup", runtimeSessionId, userId, actorId)
    → Container init(userId, "web:sub_xxx", "web") → scoped credentials
    ← {sessionId, wsEndpoint, runtimeArn}

Phase 2: WebSocket Direct Connect (zero container changes)
  Browser → wss://bedrock-agentcore.<region>.amazonaws.com/runtimes/<arn>/ws
            ?X-Amzn-Bedrock-AgentCore-Runtime-Session-Id=ses_xxx
            (SigV4 presigned URL or OAuth Bearer token)
    → AgentCore platform auto-discovers container WS listener (port 18789)
    → Direct bridge to OpenClaw Gateway
    → Full Gateway Protocol v3 (97 methods, 19 events)
```

**Key facts**:
- Platform auto-discovers WebSocket listeners inside the container — no `/ws` handler needed on 8080
- Session stickiness via `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header
- 32KB frame size limit from platform (OpenClaw Gateway allows 25MB internally)
- Gateway token from Secrets Manager used for OpenClaw `connect` handshake auth
- Browser can use `base64UrlBearerAuthorization` subprotocol for OAuth Bearer in WSS

**Why two phases**: AgentCore WebSocket carries no `userId`/`actorId` — only `session_id`. HTTP bootstrap provides identity for `init()` + scoped credentials. WS then routes to the already-initialized container.
</pattern>
