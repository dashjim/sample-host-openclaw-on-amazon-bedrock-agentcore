# Web UI Channel Design: OpenClaw Gateway Protocol over AgentCore WebSocket

## Situation

OpenClaw on AgentCore Runtime is a multi-channel AI messaging platform running per-user serverless containers on AWS. Currently it supports three messaging channels (Telegram, Slack, Feishu), all following the same pattern:

```
Channel Webhook → Router Lambda → invoke_agent_runtime(HTTP) → Container /invocations
                                                                    ↓
                                                              agentcore-contract.js
                                                                    ↓
                                                              OpenClaw Gateway (WS, port 18789)
```

The container runs `openclaw gateway run --port 18789`, which is a **full OpenClaw Gateway server** supporting the complete Gateway Protocol over WebSocket. However, the current `agentcore-contract.js` bridge only uses two methods from this protocol: `connect` + `chat.send` (in the `bridgeMessage()` function). The vast majority of the Gateway Protocol surface (sessions management, chat history, agent file CRUD, skill management, cron, approvals, tool catalog, etc.) is available inside the container but completely unexposed to users.

There is no Web UI. Users can only interact through Telegram, Slack, or Feishu — text-in, text-out, with no access to advanced Gateway features like session browsing, workspace files, conversation history, or real-time streaming deltas.

### Existing authentication + isolation chain analysis

The current system has 5 security layers, each with a distinct responsibility:

```
  Telegram/Slack/Feishu App
         │
    ① Webhook signature validation (Telegram secret_token / Slack HMAC-SHA256 / Feishu AES)
         │
    ② DynamoDB user resolution + allowlist check
         │  Router Lambda: resolve_user("telegram", "123456")
         │  → lookup CHANNEL#telegram:123456 → USER#user_abc
         │  → new users: check ALLOW#telegram:123456 exists
         │
    ③ AgentCore platform auth (IAM SigV4)
         │  invoke_agent_runtime(runtimeSessionId, runtimeUserId)
         │
    ④ Per-user session isolation
         │  runtimeSessionId → dedicated microVM
         │  one container instance per user, process-level isolation
         │
    ⑤ Scoped STS credentials (S3 namespace isolation)
         │  init(userId, actorId, channel)
         │  → namespace = actorId.replace(/:/g, "_")  // "telegram_123456"
         │  → STS AssumeRole + session policy: s3:* restricted to {namespace}/*
         │  → OpenClaw process only holds scoped credentials, no container-level AWS creds
```

**Web UI reusability assessment per layer**:

| Layer | Current mechanism | Web UI reuse approach | Change required |
|---|---|---|---|
| ① Channel auth | Webhook signature (HMAC) | Cognito JWT (SRP / IdP federation) | New — different auth mode |
| ② User resolution | `resolve_user(channel, id)` + allowlist | **Full reuse** — `resolve_user("web", cognito_sub)` | None |
| ③ Platform auth | IAM (Lambda role) | OAuth Bearer (AgentCore JWT authorizer) | Config change |
| ④ Session isolation | `runtimeSessionId` → per-user microVM | **Full reuse** — `Session-Id` in WS header | None |
| ⑤ Scoped credentials | `createScopedCredentials(namespace)` | **Full reuse** — namespace = `web_{cognito_sub}` | None |

### Existing user registration and allowlist mechanism

The system uses a **controlled registration** model (`registration_open` defaults to `false`):

```python
# lambda/router/index.py — is_user_allowed()
def is_user_allowed(channel, channel_user_id):
    if REGISTRATION_OPEN:           # cdk.json config, default false
        return True
    channel_key = f"{channel}:{channel_user_id}"
    resp = identity_table.get_item(Key={"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"})
    return "Item" in resp
```

**Registration flow**:
1. New user messages the bot → bot replies with rejection + user ID (e.g. `telegram:123456`)
2. Admin runs `./scripts/manage-allowlist.sh add telegram:123456`
3. User messages again → `resolve_user()` creates `CHANNEL#` + `USER#` records → registered

**DynamoDB record structure**:
- Allowlist: `PK=ALLOW#telegram:123456, SK=ALLOW`
- Channel mapping: `PK=CHANNEL#telegram:123456, SK=PROFILE` → `{userId: "user_abc"}`
- User profile: `PK=USER#user_abc, SK=PROFILE`
- Cross-channel binding bypasses allowlist (already-authenticated user linking new channel)

### Cognito's three roles in the system

| Cognito Pool | Purpose | User-facing? | Auth flow |
|---|---|---|---|
| `openclaw-identity-pool` | **Internal** — proxy auto-creates per-actorId users with HMAC-derived passwords (`HMAC-SHA256(secret, actorId).slice(0,32)`), generates JWT for OpenClaw | Invisible | `AdminInitiateAuth` (server-side) |
| `openclaw-admin-users` | Admin UI control panel login | Admin-facing | User password auth |
| `openclaw-web-users` (new, this design) | Web UI end-user login, supports enterprise IdP federation | User-facing | SRP / auth code / IdP redirect |

All three pools are fully independent. The internal pool is transparent to Web UI design — the Web UI's JWT is consumed at the API Lambda layer and never enters the container's internal Cognito chain.

### Key constraints discovered during research

| Constraint | Detail |
|---|---|
| **OpenClaw Gateway Protocol** | WebSocket-only transport. JSON text frames. Full RPC surface: `sessions.*`, `chat.*`, `agents.files.*`, `cron.*`, `skills.*`, `tools.*`, `device.*`, etc. |
| **AgentCore Runtime WebSocket** | Native `/ws` endpoint support. Container implements WebSocket handler on port 8080 at `/ws`. Platform routes browser connections via `wss://bedrock-agentcore.<region>.amazonaws.com/runtimes/<arn>/ws`. Supports SigV4, Pre-signed URL, OAuth Bearer authentication. Session stickiness via `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header. 32KB frame size limit. |
| **Container architecture** | OpenClaw Gateway on port 18789 (loopback only). Contract server on port 8080 (`/ping` + `/invocations`). Proxy on port 18790. Per-user scoped STS credentials created during `init()`. |
| **AgentCore WS limitation** | Platform forwards raw WebSocket frames to container. No `userId`/`actorId` in WS context — only `session_id` header available. **Solution**: HTTP bootstrap phase passes identity via `invoke_agent_runtime` and completes `init()` first; WS only needs to route to the already-initialized container via matching `session_id` |
| **Per-user isolation** | Each user gets their own AgentCore session (microVM). `init(userId, actorId, channel)` creates namespace-scoped STS credentials restricting S3/DynamoDB access to user's prefix. |
| **Cognito's internal role** | Existing `openclaw-identity-pool` is a **backend-only component** — proxy auto-creates users with HMAC-derived passwords for internal JWT. End users never touch it. Web UI needs a separate user-facing Cognito Pool |
| **OpenClaw Gateway is already in the container** | `openclaw gateway run --port 18789` is the full Gateway server. Current `bridgeMessage()` only uses `connect` + `chat.send` (tip of the iceberg). Browser cannot reach 18789 directly (loopback), needs transparent bridge at 8080 `/ws` |

---

## Task

Design a Web UI channel that:

1. **Exposes the full OpenClaw Gateway Protocol** to browser clients — not just chat, but session management, file CRUD (`agents.files.*`), conversation history (`chat.history`), cron scheduling, skill management, auto-repair, and all other RPC families
2. **Reuses existing per-user isolation** — DynamoDB identity resolution, session management, scoped STS credentials, S3 namespace enforcement
3. **Minimizes container-side changes** — ideally only adding a `/ws` handler to `agentcore-contract.js`
4. **Supports enterprise IdP federation** — corporate SAML/OIDC providers can be plugged in without modifying the internal authentication chain
5. **Enables real-time streaming** — token-by-token response deltas, session events, presence, tool execution progress

### Target UI capabilities (reference: OpenClaw Control UI)

| Feature | Gateway Protocol methods |
|---|---|
| Chat with streaming deltas | `chat.send`, `chat.history`, `chat.abort`, `chat.inject` |
| Session management | `sessions.list`, `sessions.create`, `sessions.delete`, `sessions.compact` |
| Workspace files | `agents.files.list`, `agents.files.get`, `agents.files.set` |
| Scheduled tasks (cron) | `cron.list`, `cron.add`, `cron.update`, `cron.remove`, `cron.run` |
| Skill/tool management | `skills.status`, `skills.install`, `skills.search`, `tools.catalog` |
| System status | `health`, `status`, `diagnostics.stability` |
| Model selection | `models.list` |
| Config management | `config.get`, `config.set`, `config.patch` |
| Auto-repair | `sessions.steer`, `sessions.abort` |

---

## Action

### Architecture: HTTP Bootstrap + WebSocket Bridge

The core insight: solve the WebSocket identity gap by **splitting the connection lifecycle into two phases** — reuse the existing HTTP invocation for identity/init, then upgrade to WebSocket for real-time Gateway Protocol access.

```
Phase 1: HTTP Bootstrap (reuses ALL existing logic)
══════════════════════════════════════════════════════

  Browser                  Web API Lambda              AgentCore Platform
    │                          │                              │
    │── POST /api/session ────→│                              │
    │   (Cognito JWT)          │── resolve_user() ──→ DynamoDB│
    │                          │← user_id, session_id ────────│
    │                          │                              │
    │                          │── invoke_agent_runtime() ───→│
    │                          │   action: "warmup"           │
    │                          │   runtimeSessionId: ses_xxx  │──→ Container /invocations
    │                          │   userId, actorId, channel   │    init(userId, actorId, "web")
    │                          │                              │    → scoped credentials created
    │                          │                              │    → OpenClaw Gateway started
    │                          │←── {status: "ready"} ────────│
    │                          │                              │
    │←── {sessionId, wsUrl} ───│                              │
    │                          │                              │

Phase 2: WebSocket Gateway Protocol (new, minimal container change)
════════════════════════════════════════════════════════════════════

  Browser                              AgentCore Platform        Container
    │                                        │                      │
    │── WSS connect ────────────────────────→│                      │
    │   wss://bedrock-agentcore.../ws        │                      │
    │   ?Session-Id=ses_xxx                  │──── WS upgrade ────→│ /ws handler
    │   (OAuth Bearer token)                 │   (same session!)    │
    │                                        │                      │
    │←─────────────── WS connected ──────────│←─────────────────────│
    │                                        │                      │
    │── Gateway Protocol frames ────────────────────────────────────│
    │   {type:"req", method:"connect", ...}  │                      │──→ ws://127.0.0.1:18789
    │                                        │                      │    (OpenClaw Gateway)
    │←── {type:"res", ok:true, ...} ─────────│←─────────────────────│
    │                                        │                      │
    │── {method:"chat.send", ...} ──────────────────────────────────│──→ OpenClaw Gateway
    │←── {event:"chat", state:"delta"} ─────────────────────────────│    full protocol
    │←── {event:"chat", state:"delta"} ─────────────────────────────│
    │←── {event:"chat", state:"final"} ─────────────────────────────│
    │                                        │                      │
    │── {method:"agents.files.list"} ───────────────────────────────│──→ OpenClaw Gateway
    │←── {files: [...]} ────────────────────────────────────────────│
    │                                        │                      │
    │── {method:"cron.list"} ───────────────────────────────────────│──→ OpenClaw Gateway
    │←── {schedules: [...]} ────────────────────────────────────────│
```

### Component breakdown

#### 1. Web Auth Cognito User Pool (new CDK resource)

A **separate, user-facing Cognito User Pool** for Web UI authentication. Distinct from the internal `openclaw-identity-pool` used by the proxy.

```python
# stacks/security_stack.py — new resource
self.web_user_pool = cognito.UserPool(
    self, "WebUserPool",
    user_pool_name="openclaw-web-users",
    self_sign_up_enabled=False,           # Admin-provisioned or IdP-federated
    sign_in_aliases=cognito.SignInAliases(username=True, email=True),
)

# Federation: plug in corporate IdP here
# self.web_user_pool.register_identity_provider(
#     cognito.UserPoolIdentityProviderOidc(...)  # Okta, Azure AD, etc.
#     cognito.UserPoolIdentityProviderSaml(...)  # ADFS, etc.
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

**Why separate from internal Cognito**: The internal pool uses `AdminCreateUser` + HMAC-derived passwords — a machine-to-machine pattern unsuitable for human login. The web pool supports standard flows (SRP, authorization code) and IdP federation.

**Enterprise IdP integration path**:
- **SAML**: Register `UserPoolIdentityProviderSaml` (ADFS, Azure AD)
- **OIDC**: Register `UserPoolIdentityProviderOidc` (Okta, Auth0, Google Workspace)
- **Social**: Built-in Cognito adapters (Google, Facebook, Apple)
- Zero changes to container code — the token format is the same JWT regardless of upstream IdP

#### 2. AgentCore Runtime OAuth Authorizer (new configuration)

Configure the AgentCore Runtime Endpoint to accept the Web Cognito User Pool's JWTs:

```python
# Via AWS SDK (Starter Toolkit does not expose this yet)
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

This allows browsers to connect directly to AgentCore WebSocket using Cognito JWT — both for the HTTP bootstrap (via API Lambda with IAM) and for direct WebSocket connections (via OAuth Bearer).

#### 3. Web API Lambda (new, thin layer on existing Router Lambda)

A lightweight Lambda handling the HTTP bootstrap phase. It reuses the Router Lambda's core functions (user resolution, session management, AgentCore invocation) but with Web-specific authentication:

```python
# lambda/web_api/index.py

def handler(event, context):
    """Web UI API — session bootstrap and user management."""
    path = event["rawPath"]
    method = event["requestContext"]["http"]["method"]

    if method == "POST" and path == "/api/session":
        return handle_create_session(event)
    if method == "GET" and path == "/api/session":
        return handle_get_session(event)
    if method == "POST" and path == "/api/link":
        return handle_link_channel(event)

def handle_create_session(event):
    """Bootstrap: resolve user → warmup AgentCore → return session info."""
    # 1. Extract Cognito JWT claims
    jwt_claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    cognito_sub = jwt_claims["sub"]
    actor_id = f"web:{cognito_sub}"

    # 2. Resolve or create user in DynamoDB (reuse existing logic)
    user_id = resolve_user(actor_id)

    # 3. Get or create AgentCore session (reuse existing logic)
    session_id = get_or_create_session(user_id)

    # 4. Warmup the container (triggers init with identity + scoped creds)
    invoke_agent_runtime(
        session_id=session_id,
        user_id=user_id,
        actor_id=actor_id,
        channel="web",
        message=None,
        action="warmup",
    )

    # 5. Return session info for WebSocket connection
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

API Gateway HTTP API with Cognito JWT authorizer — no custom auth code needed.

#### 4. Container `/ws` Handler (minimal change to `agentcore-contract.js`)

The only container-side change: a transparent WebSocket-to-WebSocket bridge from AgentCore's `/ws` to OpenClaw Gateway's port 18789.

```javascript
// agentcore-contract.js — new /ws handler (added to existing HTTP server)

server.on("upgrade", (req, socket, head) => {
  if (req.url !== "/ws") {
    socket.destroy();
    return;
  }

  // Container is already initialized via HTTP warmup — just bridge
  if (!openclawReady) {
    socket.write("HTTP/1.1 503 Service Unavailable\r\n\r\n");
    socket.destroy();
    return;
  }

  // Create upstream connection to OpenClaw Gateway
  const upstream = new WebSocket(`ws://127.0.0.1:${OPENCLAW_PORT}`, {
    origin: `http://127.0.0.1:${OPENCLAW_PORT}`,
  });

  // Accept the downstream (AgentCore platform → browser) connection
  const wss = new WebSocket.Server({ noServer: true });
  wss.handleUpgrade(req, socket, head, (downstream) => {
    // Bidirectional frame relay — zero parsing, zero transformation
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

    // Lifecycle management
    downstream.on("close", () => upstream.close());
    upstream.on("close", () => downstream.close());
    downstream.on("error", () => upstream.close());
    upstream.on("error", () => downstream.close());
  });
});
```

Key design decisions:
- **Zero frame parsing** — raw bidirectional relay. The Gateway Protocol is between browser and OpenClaw Gateway; the bridge is transparent
- **No auth in the bridge** — authentication already handled at two layers: AgentCore platform (OAuth JWT) and OpenClaw Gateway (token auth via `connect` handshake)
- **No init logic** — HTTP bootstrap guarantees the container is initialized before WebSocket connects
- **32KB AgentCore frame limit** — OpenClaw Gateway's default `maxPayload` is 25MB, but AgentCore caps at 32KB. Large `agents.files.set` payloads need chunking (handled at the client SDK level)

#### 5. Web UI Frontend (React SPA on S3+CloudFront)

Extends the existing admin-ui infrastructure pattern. The frontend connects to OpenClaw Gateway Protocol over the AgentCore WebSocket bridge.

```
admin-ui/           (existing — admin control plane)
web-ui/             (new — user-facing chat + workspace UI)
  src/
    services/
      auth.ts         # Cognito auth (Amplify v6, same pattern as admin-ui)
      gateway.ts      # Gateway Protocol WebSocket client
      bootstrap.ts    # HTTP session bootstrap (POST /api/session)
    pages/
      Chat.tsx        # Chat interface with streaming deltas
      Sessions.tsx    # Session list, create, delete, compact
      Files.tsx       # Workspace file browser (agents.files.*)
      Cron.tsx        # Scheduled tasks management (cron.*)
      Skills.tsx      # Skill browser and installer
    components/
      MessageStream.tsx   # Real-time delta rendering
      FileEditor.tsx      # In-browser file viewer/editor
      CronScheduler.tsx   # Visual cron expression builder
```

Deployment: same S3+CloudFront+OAC pattern as `admin-ui/`, separate distribution.

#### 6. CDK Stack Changes

```python
# New stack or extend admin_stack.py
class WebUiStack(Stack):
    """Web UI channel — Cognito (user-facing), API Lambda, S3+CloudFront."""

    def __init__(self, scope, id, *, security_stack, router_stack, agentcore_stack, **kwargs):
        super().__init__(scope, id, **kwargs)

        # 1. Web Cognito User Pool (user-facing, supports IdP federation)
        #    Separate from internal openclaw-identity-pool

        # 2. API Gateway HTTP API + Cognito JWT authorizer
        #    Routes: POST /api/session, GET /api/session, POST /api/link

        # 3. Web API Lambda (thin wrapper — reuses router logic)

        # 4. S3 bucket + CloudFront distribution (React SPA)
        #    OAC for S3 origin, same pattern as admin-ui

        # 5. Output: CloudFront domain, Cognito config, WS endpoint
```

#### 7. Web User Registration and Allowlist (reuses existing mechanism + new admin script)

Web UI user registration reuses the existing DynamoDB allowlist mechanism, identical to Telegram/Slack/Feishu.

**Registration flow design**:

```
Scenario A: Admin pre-registration (recommended, consistent with existing channels)
════════════════════════════════════════════════════════════════════════════════════

  1. Admin creates Cognito user
     $ ./scripts/manage-web-users.sh add user@company.com

     → Creates user in openclaw-web-users pool
     → Gets cognito_sub (auto-generated UUID)
     → Writes ALLOW#web:{cognito_sub} to DynamoDB
     → Sends temporary password email (Cognito automatic)

  2. User first login to Web UI
     → Cognito forces password change
     → POST /api/session (JWT carries sub)
     → Web API Lambda: resolve_user("web", cognito_sub)
       → Checks ALLOW#web:{cognito_sub} ✓
       → Creates CHANNEL#web:{sub} + USER#user_xxx
     → Returns sessionId → WebSocket connection

Scenario B: Enterprise IdP federation (auto-registration)
═════════════════════════════════════════════════════════════

  1. Admin configures Cognito IdP federation (Okta/Azure AD/SAML)
  2. Admin bulk-adds allowlist
     $ ./scripts/manage-web-users.sh add-batch user-list.csv

  3. User logs in via IdP
     → Cognito Hosted UI → redirect to enterprise IdP → authenticate → callback
     → POST /api/session → resolve_user("web", cognito_sub)
     → Allowlist check → register → normal use

Scenario C: Open registration (optional)
═════════════════════════════════════════

  cdk.json: "registration_open": true
  → is_user_allowed() returns true directly
  → Any Cognito-authenticated user can register
  → Suitable for internal testing or when enterprise IdP already controls access
```

**Admin script** (`scripts/manage-web-users.sh`):

```bash
#!/bin/bash
# Web UI user management — create Cognito user + DynamoDB allowlist
#
# Usage:
#   ./scripts/manage-web-users.sh add user@company.com     # Add user
#   ./scripts/manage-web-users.sh remove user@company.com  # Remove user
#   ./scripts/manage-web-users.sh list                     # List all web users
#   ./scripts/manage-web-users.sh add-batch users.csv      # Bulk add

cmd_add() {
    local email="$1"

    # 1. Create user in Web Cognito User Pool
    local cognito_sub
    cognito_sub=$(aws cognito-idp admin-create-user \
        --user-pool-id "$WEB_USER_POOL_ID" \
        --username "$email" \
        --user-attributes Name=email,Value="$email" Name=email_verified,Value=true \
        --region "$REGION" \
        --query 'User.Attributes[?Name==`sub`].Value' \
        --output text)

    # 2. Add to DynamoDB allowlist
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

    echo "Done. $email (web:$cognito_sub) added to allowlist."
    echo "User will receive a temporary password email."
}
```

**Key design decisions**:
- Web user `actorId` format is `web:{cognito_sub}`, parallel to `telegram:{user_id}`
- Reuses `resolve_user(channel, channel_user_id)` — first arg `"web"`, second arg `cognito_sub`
- Allowlist check uses the same `is_user_allowed("web", cognito_sub)` path
- Script style consistent with existing `manage-allowlist.sh`
- Enterprise IdP scenario: if IdP already controls access (e.g. company employees only), set `registration_open: true` to skip allowlist

### Authentication flow (complete chain)

```
Browser                     Cognito             API Lambda          AgentCore        Container
  │                           │                     │                  │               │
  │── Login (SRP/IdP) ──────→│                     │                  │               │
  │←── JWT (IdToken) ─────────│                     │                  │               │
  │                           │                     │                  │               │
  │── POST /api/session ─────────────────────────→ │                  │               │
  │   Authorization: Bearer <JWT>                   │                  │               │
  │                         [API GW validates JWT]  │                  │               │
  │                                                 │── warmup ──────→│               │
  │                                                 │  (IAM SigV4)    │──/invocations→│
  │                                                 │                  │  init(userId) │
  │                                                 │                  │  scopedCreds  │
  │←── {sessionId, wsEndpoint} ─────────────────────│                  │               │
  │                                                 │                  │               │
  │── WSS connect ──────────────────────────────────────────────────→ │               │
  │   Sec-WebSocket-Protocol:                       │                  │               │
  │     base64UrlBearerAuthorization.<JWT>          │                  │               │
  │   ?Session-Id=ses_xxx                           │[OAuth validate]  │               │
  │                                                 │                  │── /ws ───────→│
  │←── WS connected ────────────────────────────────────────────────────────────────── │
  │                                                 │                  │               │
  │── Gateway Protocol ──────────────────────────────────────────────────────────────→ │
  │   (connect → chat.send → sessions.list → ...)   │                  │  ↕ bridge ↕   │
  │←── Gateway Protocol events ───────────────────────────────────────────────────────│
  │                                                 │                  │  ws://18789   │
```

**4 layers of security** (comparable to existing channels):

| Layer | Telegram/Slack | Web UI |
|---|---|---|
| Channel auth | Webhook signature (HMAC) | Cognito JWT (SRP / IdP federation) |
| Platform auth | IAM (Lambda role) | OAuth Bearer (AgentCore JWT authorizer) |
| Session isolation | `runtimeSessionId` → per-user microVM | Same — `runtimeSessionId` in WS header |
| Data isolation | Scoped STS (S3 namespace) | Same — `init()` creates scoped creds |
| Internal auth | Cognito HMAC token (proxy→OpenClaw) | Same — proxy auto-provisions |

### Cross-channel binding

Web users can bind to existing Telegram/Slack accounts:

1. User logged in via Web UI says "link accounts"
2. OpenClaw generates 6-char bind code (existing logic, stored in DynamoDB with 10-min TTL)
3. User enters code on Telegram/Slack
4. Both channels now map to same `USER#user_abc` — shared session, workspace, files, cron

Alternatively, Web UI provides a `/api/link` endpoint for programmatic binding.

---

## Result

### What this design achieves

| Capability | Status |
|---|---|
| Full Gateway Protocol access from browser | All RPC families exposed via WS bridge |
| Chat with real-time streaming deltas | `chat.send` → `session.message` events |
| Session management (create/list/delete/compact) | `sessions.*` methods |
| Workspace file browser/editor | `agents.files.list/get/set` |
| Cron scheduling UI | `cron.list/add/update/remove/run` |
| Skill/tool management | `skills.status/install/search`, `tools.catalog` |
| Auto-repair / steer | `sessions.steer`, `sessions.abort` |
| Conversation history | `chat.history` (display-normalized) |
| Enterprise IdP federation | Cognito SAML/OIDC → zero container changes |
| Cross-channel identity binding | Existing `link` mechanism, shared workspace |
| Per-user S3 isolation | Existing scoped STS credentials, no changes |

### Change footprint

| Component | Change type | Effort |
|---|---|---|
| `bridge/agentcore-contract.js` | Add `/ws` handler (~50 lines) | Small |
| `stacks/security_stack.py` | Add Web Cognito User Pool | Small |
| `stacks/web_ui_stack.py` | New stack (API GW + Lambda + S3/CF) | Medium |
| `lambda/web_api/index.py` | New Lambda (reuses router logic) | Medium |
| `scripts/manage-web-users.sh` | New admin script (Cognito + allowlist) | Small |
| AgentCore Endpoint config | Add CUSTOM_JWT authorizer | Config only |
| `web-ui/` | New React SPA | Large (but independent) |
| Existing channels | **Zero changes** | None |
| `bridge/agentcore-proxy.js` | **Zero changes** | None |
| `bridge/lightweight-agent.js` | **Zero changes** | None |
| Scoped credentials | **Zero changes** | None |

### What is NOT in scope

- Voice/audio (WebRTC) — future phase
- Multi-user collaborative sessions — single-user per session
- Canvas rendering inside the WebSocket — the `canvas` tool remains denied
- Replacing Telegram/Slack channels — Web UI is additive

---

## Appendix: Q&A

### Q1: OpenClaw Gateway 到底在不在 Docker 容器里？

**在。** `openclaw gateway run --port 18789` 就是完整的 OpenClaw Gateway 服务器。Dockerfile 安装 `openclaw@2026.3.8`（全局 npm），`agentcore-contract.js` 在 `init()` 时 spawn 这个进程。当前的 `bridgeMessage()` 只用了 `connect` + `chat.send` 两个 method，但 Gateway 原生支持完整的 Protocol surface。

### Q2: 为什么不能让浏览器直接 WebSocket 连到 OpenClaw Gateway（18789）？

因为 OpenClaw Gateway 绑定 `127.0.0.1:18789`（容器内部 loopback）。AgentCore 平台只暴露容器的 8080 端口（`/ping`、`/invocations`、`/ws`）。所以必须在 8080 的 `/ws` 做一层透明桥接。

### Q3: AgentCore WebSocket 的 32KB 帧限制会影响什么？

主要影响 `agents.files.set`（写文件）和 `chat.history`（长对话历史）。解决方案：
- 文件写入：客户端分块发送，每块 < 32KB
- 历史读取：使用 `sessions.preview` 的 bounded preview，或分页加载
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
- **SAML 2.0**: ADFS, Azure AD, OneLogin
- **OIDC**: Okta, Auth0, Google Workspace, Keycloak
- **Social**: Google, Facebook, Apple, Amazon（Cognito 内置）

用户登录流程：浏览器 → Cognito Hosted UI → 跳转企业 IdP → 认证 → 回调 → JWT token → 一切如常。

### Q5: How do new Web users register? Is admin action required?

**Yes, admin action is required** (default mode), consistent with Telegram/Slack. Flow:

1. Admin runs `./scripts/manage-web-users.sh add user@company.com` → creates Cognito user + DynamoDB allowlist entry
2. User receives temporary password email
3. User first login → Cognito forces password change → `POST /api/session` → `resolve_user("web", sub)` → allowlist check passes → user registered

**Three registration modes**:
- **Admin pre-registration** (default): `manage-web-users.sh add email` → create Cognito user + allowlist
- **Enterprise IdP federation**: Admin bulk-imports allowlist, users login via IdP SSO
- **Open registration**: `registration_open: true` → any Cognito-authenticated user can register (suitable when IdP already controls access)

Web users are independent of Telegram/Slack with actorId format `web:<cognito_sub>`. Users with both channels can bind via `link` command — shared user profile, session, workspace, files, cron.

### Q6: Cognito 在系统中有三个角色，分别是什么？

| Cognito Pool | 用途 | 用户可见？ | IdP 联邦？ |
|---|---|---|---|
| `openclaw-identity-pool` | 内部 — proxy 为每个 actorId 自动创建用户，HMAC 密码，生成 JWT 给 OpenClaw | 不可见 | 不需要 |
| `openclaw-admin-users` | Admin UI 登录 | Admin 可见 | 可选 |
| `openclaw-web-users` (新建) | Web UI 终端用户登录 | 用户可见 | 推荐 — 企业 IdP |

三个 pool 完全独立，互不干扰。

### Q7: 现有的 Scoped Credentials 如何被复用？

完全不需要改动。流程：

1. HTTP bootstrap 调 `invoke_agent_runtime(action:"warmup", userId, actorId:"web:sub_xxx")`
2. 容器 `init("user_abc", "web:sub_xxx", "web")` → `namespace = "web_sub_xxx"`
3. `createScopedCredentials("web_sub_xxx")` → STS session policy: `s3:*` 限制到 `web_sub_xxx/*`
4. 后续 WebSocket 上的 `agents.files.*` 操作都在 OpenClaw 内执行，受 scoped credentials 约束

S3 目录结构：`s3://openclaw-user-files-{account}-{region}/web_sub_xxx/`

### Q8: 如果浏览器断开 WebSocket 重连会怎样？

- **Session 存活**：AgentCore session 有 idle timeout（默认 15 min），WS 断开不立即销毁 session
- **重连流程**：浏览器直接带相同 `session_id` 重新 WSS 连接 → 路由到同一容器 → `/ws` bridge 重建到 OpenClaw Gateway → Gateway 协议重新握手（`connect`）
- **状态恢复**：OpenClaw Gateway 维护 session 状态，重连后 `chat.history` 返回完整对话历史
- **不需要重新 bootstrap**：容器已初始化，scoped credentials 有效（45 min 刷新），直接 WS 重连即可

### Q9: HTTP Bootstrap 的 warmup 和现有 Telegram 流程有什么区别？

几乎没有区别：

| 步骤 | Telegram | Web UI |
|---|---|---|
| 用户解析 | Router Lambda `resolve_user("telegram:123")` | Web API Lambda `resolve_user("web:sub_xxx")` |
| Session 创建 | `get_or_create_session(user_id)` | 同 |
| AgentCore 调用 | `invoke_agent_runtime(action:"chat")` | `invoke_agent_runtime(action:"warmup")` |
| 容器 init | `init(userId, "telegram:123", "telegram")` | `init(userId, "web:sub_xxx", "web")` |
| Scoped creds | `createScopedCredentials("telegram_123")` | `createScopedCredentials("web_sub_xxx")` |
| 后续交互 | HTTP invocation (chat action) | WebSocket (Gateway Protocol) |

唯一区别：Telegram 后续消息也走 HTTP invocation，Web UI 后续消息走 WebSocket 直连。

### Q10: 现有 channel（Telegram/Slack/Feishu）是否受影响？

**零影响。** Web UI 是一个完全独立的新 channel：
- 独立的 Cognito User Pool
- 独立的 API Gateway
- 独立的 Lambda
- 容器侧只新增 `/ws` handler，不修改 `/invocations`
- 现有 `bridgeMessage()`、`chat` action、`cron` action 全部保持不变

### Q11: Why HTTP bootstrap + WS two-phase instead of pure WebSocket?

Core reason: the AgentCore WebSocket protocol **does not carry userId/actorId** — only `session_id`.

With pure WS, the container `/ws` handler receives anonymous frames — it doesn't know who connected and cannot call `init(userId, actorId, channel)` to create scoped credentials.

HTTP bootstrap solves this:
1. `invoke_agent_runtime(action:"warmup")` payload includes `userId`, `actorId`, `channel`
2. Container completes `init()` → scoped credentials in place, OpenClaw Gateway started
3. WS routes to the same initialized container via matching `session_id` → no identity needed
4. All subsequent Gateway Protocol frames are bridged transparently, zero parsing on container side

**Bonus**: HTTP bootstrap fully reuses the existing Router Lambda's `resolve_user()` + `get_or_create_session()` + `invoke_agent_runtime()` — no need to reimplement user resolution or allowlist checking in the container.

### Q12: Does enterprise IdP integration require container code changes?

**Not at all.** IdP changes only affect Cognito configuration (CDK level):

```
User → Cognito Hosted UI → Enterprise IdP (Okta/Azure AD/ADFS) → authenticate
  → callback to Cognito → JWT (standard format, sub from IdP mapping)
  → API Lambda consumes JWT → invoke_agent_runtime → container
```

The container always receives `{action:"warmup", userId:"user_abc", actorId:"web:xxx"}` — it doesn't care whether the JWT came from Cognito native auth or IdP federation. All three Cognito Pools (internal, Admin, Web) operate independently.
