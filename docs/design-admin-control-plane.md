# Admin Control Plane — Design Document

**Date**: 2026-03-20
**Branch**: `feature/admin-control-plane`
**Status**: Draft

## Overview

Add a serverless admin control plane to the OpenClaw on AgentCore project. The control plane provides a web UI for administrators to manage channel integrations (Telegram, Slack, Feishu), users, allowlists, and per-user S3 files — replacing the current CLI-only workflows (`setup-telegram.sh`, `setup-slack.sh`, `manage-allowlist.sh`).

## Goals

1. **Channel Management** — Configure Telegram/Slack/Feishu bot tokens and webhook registration via UI (currently requires CLI + Secrets Manager)
2. **User Management** — View, add, and delete users and allowlist entries; view cross-channel bindings; manage individual channel access
3. **File Management** — Browse and delete per-user S3 files (both `.openclaw/` workspace and user-created files)
4. **Dashboard** — At-a-glance stats: user count, channel distribution, channel config status
5. **Admin Authentication** — Secure login/logout via a dedicated Cognito User Pool (separate from the bot identity pool)

## Non-Goals

- Modifying OpenClaw runtime configuration (model ID, session timeouts, etc.)
- Viewing or managing AgentCore runtime/sessions directly
- Real-time log viewing or monitoring (existing CloudWatch dashboards serve this)
- Multi-tenant admin (single admin pool for the deployment)

## Architecture

```
                        ┌─────────────────────┐
                        │   CloudFront + S3    │
                        │   (React + Antd SPA) │
                        └──────────┬──────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    │   Cognito User Pool (Admin)  │
                    │   (Separate from bot pool)   │
                    │   Email verification, MFA    │
                    └──────────────┬──────────────┘
                                   │ JWT (ID token)
                        ┌──────────┴──────────┐
                        │    API Gateway       │
                        │    (HTTP API)        │
                        │  Cognito Authorizer  │
                        └──────────┬──────────┘
                                   │
                        ┌──────────┴──────────┐
                        │   Admin Lambda       │
                        │   (Python, single    │
                        │    function, routed) │
                        └──────────┬──────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                     │
     ┌────────┴────────┐  ┌───────┴────────┐  ┌────────┴────────┐
     │   DynamoDB       │  │ Secrets Manager │  │    S3 Bucket    │
     │ openclaw-identity│  │ Channel tokens  │  │ User files +    │
     │                  │  │                 │  │ workspace       │
     └─────────────────┘  └────────────────┘  └─────────────────┘
```

### Why a Separate Cognito User Pool?

The existing `openclaw-identity-pool` is designed for **bot service identities** — users are auto-provisioned with deterministic HMAC-derived passwords, no email verification, no MFA, no password recovery. This is fundamentally incompatible with human admin login requirements:

| Concern | Bot Pool | Admin Pool |
|---------|----------|------------|
| User creation | Automatic (AdminCreateUser) | Manual (setup script) |
| Password | HMAC-derived, deterministic | User-chosen, forced change on first login |
| Email | Not configured | Required, verified |
| MFA | N/A (service identity) | Optional (TOTP) |
| Password recovery | None | Email-based |
| Auth flow | ADMIN_USER_PASSWORD_AUTH | USER_PASSWORD_AUTH + OAuth2 |

### Why Not ECS?

The original plan called for ECS, but the admin workload is a perfect fit for serverless:

- Low, bursty traffic (admin use only, not user-facing)
- No persistent connections or state
- Consistent with existing project Lambda patterns
- Lower cost (no idle container charges)
- Simpler operations (no ECS cluster, task definitions, ALB)

## Detailed Design

### CDK Stack: `OpenClawAdmin`

**Dependencies**: Security (KMS CMK), Router (DynamoDB table name + Router API URL), AgentCore (S3 bucket name)

**Deployment phase**: Phase 3 — after Router stack is deployed (same phase as OpenClawCron). The admin stack imports the Router stack's `ApiUrl` output and the identity table name using deterministic string ARNs (same pattern as the Cron stack) to avoid cyclic dependencies.

#### Resources

| Resource | Name | Description |
|----------|------|-------------|
| Cognito User Pool | `openclaw-admin-pool` | Admin-only, email verification, 12+ char password, optional TOTP MFA |
| Cognito App Client | `openclaw-admin-client` | USER_PASSWORD_AUTH, OAuth2 code flow, no client secret |
| Lambda Function | `openclaw-admin-api` | Python 3.12, 256 MB, 60s timeout, single function with path-based routing |
| API Gateway HTTP API | `openclaw-admin-api-gw` | Cognito JWT Authorizer on all `/api/*` routes |
| S3 Bucket | `openclaw-admin-frontend-{account}-{region}` | Static SPA assets, private (OAC only) |
| CloudFront Distribution | — | OAC to S3, custom error response (403/404 → `/index.html` with 200) for SPA routing, HTTPS only |

#### JWT Authorizer Configuration

The API Gateway HTTP API JWT Authorizer is configured with:
- **Issuer**: `https://cognito-idp.{region}.amazonaws.com/{adminUserPoolId}`
- **Audience**: `[adminClientId]` (the admin app client ID)
- **Token source**: `$request.header.Authorization` (Bearer token)
- **Token type**: ID token (Cognito ID token has `aud` = client ID, which matches the authorizer audience)

#### Lambda Environment Variables

```
IDENTITY_TABLE_NAME    = openclaw-identity
S3_USER_FILES_BUCKET   = openclaw-user-files-{account}-{region}
WEBHOOK_SECRET_ID      = openclaw/webhook-secret
TELEGRAM_SECRET_ID     = openclaw/channels/telegram
SLACK_SECRET_ID        = openclaw/channels/slack
FEISHU_SECRET_ID       = openclaw/channels/feishu
ROUTER_API_URL         = {Router API Gateway URL, imported from Router stack output}
```

#### Lambda IAM Policy

```yaml
- Effect: Allow
  Action:
    - dynamodb:Scan
    - dynamodb:Query
    - dynamodb:GetItem
    - dynamodb:PutItem
    - dynamodb:DeleteItem
  Resource:
    - arn:aws:dynamodb:{region}:{account}:table/openclaw-identity

- Effect: Allow
  Action:
    - secretsmanager:GetSecretValue
    - secretsmanager:PutSecretValue
  Resource:
    - arn:aws:secretsmanager:{region}:{account}:secret:openclaw/channels/*

- Effect: Allow
  Action:
    - secretsmanager:GetSecretValue
  Resource:
    - arn:aws:secretsmanager:{region}:{account}:secret:openclaw/webhook-secret*

- Effect: Allow
  Action:
    - s3:ListBucket
  Resource:
    - arn:aws:s3:::openclaw-user-files-{account}-{region}

- Effect: Allow
  Action:
    - s3:GetObject
    - s3:DeleteObject
  Resource:
    - arn:aws:s3:::openclaw-user-files-{account}-{region}/*

- Effect: Allow
  Action:
    - scheduler:DeleteSchedule
  Resource:
    - arn:aws:scheduler:{region}:{account}:schedule/openclaw-cron/*

- Effect: Allow
  Action:
    - kms:Decrypt
    - kms:GenerateDataKey
  Resource:
    - {CMK ARN}
```

**Note**: Webhook secret is read-only (GetSecretValue only) — admin cannot modify it, which would break webhook validation for all channels. Channel secrets are read-write (GetSecretValue + PutSecretValue). `kms:Encrypt` is not needed — Secrets Manager uses `kms:GenerateDataKey` for envelope encryption.

**Accepted risk**: The admin Lambda has `dynamodb:Scan` on the identity table, which includes `BIND#` records (cross-channel binding codes with 10-min TTL). This is acceptable given admin-only access.

### API Design

All endpoints require a valid Cognito ID token in the `Authorization: Bearer <token>` header. The API Gateway Cognito Authorizer validates the token before the Lambda is invoked.

**Pagination**: All list endpoints support `?nextToken=X&limit=N` query parameters. Default limit is 50 for DynamoDB-backed endpoints and 100 for S3-backed endpoints. Responses include a `nextToken` field when more results are available.

**Audit logging**: Every mutating operation (PUT, POST, DELETE) emits a structured CloudWatch log entry with the admin's Cognito `sub` claim (extracted from the JWT), the action performed, and the target resource. API Gateway access logging is also enabled.

**URL encoding**: Path parameters containing colons (e.g., `telegram:123456`) must be URL-encoded (e.g., `telegram%3A123456`). The Lambda URL-decodes all path parameters.

#### Channel Management

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/channels` | List all channels with configuration status and webhook URL |
| `PUT` | `/api/channels/{channel}` | Update channel credentials in Secrets Manager |
| `DELETE` | `/api/channels/{channel}` | Reset channel credentials to empty placeholder |
| `POST` | `/api/channels/telegram/webhook` | Register Telegram webhook (calls Telegram setWebhook API) |

**Channel status detection**: Read the Secrets Manager value. If it equals the 32-character CDK-generated placeholder, the channel is "not configured". Otherwise, it is "configured".

**GET `/api/channels` response** includes the webhook URL for each channel so admins can copy it for manual Slack/Feishu Event Subscriptions setup:

```json
{
  "channels": [
    {
      "name": "telegram",
      "configured": true,
      "webhookUrl": "https://xxx.execute-api.us-west-2.amazonaws.com/webhook/telegram"
    },
    {
      "name": "slack",
      "configured": true,
      "webhookUrl": "https://xxx.execute-api.us-west-2.amazonaws.com/webhook/slack"
    },
    {
      "name": "feishu",
      "configured": false,
      "webhookUrl": "https://xxx.execute-api.us-west-2.amazonaws.com/webhook/feishu"
    }
  ]
}
```

**PUT `/api/channels/{channel}` request body**:

```json
// Telegram
{ "botToken": "123456:ABC-DEF..." }

// Slack
{ "botToken": "xoxb-...", "signingSecret": "a1b2c3d4..." }

// Feishu
{ "appId": "cli_...", "appSecret": "...", "verificationToken": "...", "encryptKey": "..." }
```

**POST `/api/channels/telegram/webhook` logic**:
1. Read Telegram bot token from Secrets Manager
2. Read webhook secret from Secrets Manager
3. Get Router API URL from `ROUTER_API_URL` environment variable
4. Call `https://api.telegram.org/bot{token}/setWebhook?url={apiUrl}webhook/telegram&secret_token={webhookSecret}`
5. Return Telegram API response

**Note**: Slack and Feishu webhook registration is handled automatically by the Router Lambda's `url_verification` challenge handler. The admin only needs to copy the webhook URL from the channels page and paste it into the Slack/Feishu developer console. The UI will display setup instructions for each channel.

#### User Management

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/users` | List all users with bound channels |
| `GET` | `/api/users/{userId}` | User detail: profile, channels, session, cron jobs |
| `DELETE` | `/api/users/{userId}` | Delete user and all associated records |
| `DELETE` | `/api/users/{userId}/channels/{channelKey}` | Unbind a specific channel from a user |
| `GET` | `/api/allowlist` | List all allowlist entries |
| `POST` | `/api/allowlist` | Add allowlist entry |
| `DELETE` | `/api/allowlist/{channelKey}` | Remove allowlist entry |

**GET `/api/users` implementation** (optimized single-scan approach):
1. Scan DynamoDB with `FilterExpression: PK begins_with USER#`
2. This returns all USER# records in one pass (PROFILE, CHANNEL#*, SESSION, CRON#*)
3. Aggregate client-side: group by PK, extract profile + channels per user
4. Paginate using DynamoDB `ExclusiveStartKey` / `LastEvaluatedKey`
5. Return with `nextToken` for pagination

**DELETE `/api/users/{userId}` cleanup sequence**:
1. Query all records under `PK = USER#{userId}` (PROFILE, CHANNEL#*, SESSION, CRON#*)
2. For each `CHANNEL#` record, delete the corresponding `CHANNEL#{channelKey} PROFILE` record
3. For each `CRON#` record, delete the corresponding EventBridge Scheduler schedule (`scheduler:DeleteSchedule` on `openclaw-cron/{scheduleName}`)
4. Delete all `USER#{userId}` records (batch delete)
5. Delete any `ALLOW#` records for the user's channel keys
6. **Note**: S3 files are NOT auto-deleted on user deletion. Admin can manually clean up via the Files page if desired.

**POST `/api/allowlist` request body**:
```json
{ "channelKey": "telegram:123456789" }
```

#### File Management

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/files` | List all user namespaces (S3 top-level prefixes) |
| `GET` | `/api/files/{namespace}` | List files under a user's namespace |
| `GET` | `/api/files/{namespace}/{path+}` | Get file content (text) or presigned URL (binary) |
| `DELETE` | `/api/files/{namespace}/{path+}` | Delete a file |

**Namespace enumeration**: Use `s3:ListObjectsV2` with `Delimiter=/` to list top-level prefixes. Each prefix is a user namespace (e.g., `telegram_123456789/`). Paginated with `ContinuationToken`.

**File content**: For text files (`.md`, `.json`, `.txt`, `.js`, etc.), return content inline (max 1 MB). For binary files or files > 1 MB, return a presigned S3 URL (5-minute expiry).

**Path traversal prevention**: Validate that `{namespace}` matches `^[a-zA-Z0-9_-]+$` and `{path+}` contains no `..` segments.

#### Dashboard

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/stats` | Aggregate statistics |

**Implementation**: Single DynamoDB Scan with `FilterExpression: PK begins_with USER# OR PK begins_with ALLOW#`. Aggregate client-side: count USER# PROFILE records for total users, count CHANNEL# SK records per channel type for distribution, count ALLOW# records for allowlisted. Check Secrets Manager for channel config status (cached for 60s).

**Response**:
```json
{
  "totalUsers": 12,
  "totalAllowlisted": 15,
  "channelDistribution": {
    "telegram": 8,
    "slack": 5,
    "feishu": 2
  },
  "channels": {
    "telegram": { "configured": true },
    "slack": { "configured": true },
    "feishu": { "configured": false }
  }
}
```

**Note**: `activeSessions` removed from stats — there is no reliable way to count truly active AgentCore sessions from DynamoDB alone (SESSION records persist beyond container termination). Counting DynamoDB SESSION records would be misleading.

### Frontend Design

React + Vite + Ant Design SPA. Deployed as static files to S3, served via CloudFront.

#### Build-time Configuration

The SPA needs the API Gateway URL and Cognito pool/client IDs at build time. These are injected via environment variables during `npm run build`:

```bash
VITE_API_URL=https://xxx.execute-api.us-west-2.amazonaws.com
VITE_COGNITO_USER_POOL_ID=us-west-2_XXXXXXX
VITE_COGNITO_CLIENT_ID=XXXXXXXXXXXXXXXX
VITE_COGNITO_REGION=us-west-2
```

The deploy script (`deploy-admin-ui.sh`) reads these from CloudFormation outputs automatically.

#### Page Structure

```
┌──────────────────────────────────────────────────────┐
│  OpenClaw Admin              [admin@email.com] [Exit] │
├──────────────┬───────────────────────────────────────┤
│              │                                        │
│  Dashboard   │   (Content Area)                       │
│  Channels    │                                        │
│  Users       │                                        │
│  Files       │                                        │
│              │                                        │
└──────────────┴───────────────────────────────────────┘
```

#### Pages

**1. Login** (`/login`)
- Email + password form
- First login: forced password change flow
- Redirect to Dashboard on success

**2. Dashboard** (`/`)
- Stat cards: Total Users, Allowlisted Users
- Channel distribution bar/pie chart
- Channel status cards (green = configured, gray = not configured)

**3. Channels** (`/channels`)
- Three cards: Telegram, Slack, Feishu
- Each card shows: status badge, webhook URL (copyable)
- Click to expand configuration form:
  - **Telegram**: Bot Token input + "Register Webhook" button
  - **Slack**: Bot Token + Signing Secret inputs + setup instructions for Event Subscriptions
  - **Feishu**: App ID + App Secret + Verification Token + Encrypt Key inputs + setup instructions
- Save button writes to Secrets Manager via API
- Clear button resets to unconfigured

**4. Users** (`/users`)
- Table: User ID, Display Name, Channels (tags), Created At, Actions
- Actions: View Detail, Delete
- "Add to Allowlist" button (modal: enter channel key like `telegram:123456`)
- User detail drawer:
  - Profile info
  - Bound channels list with individual "Unbind" buttons
  - Active session info (session ID, created at, last activity)
  - Cron schedules table (name, expression, timezone, channel)
- Search by user ID or display name
- Filter by channel type
- Pagination controls

**5. Files** (`/files`)
- Left panel: Namespace list (user namespaces from S3)
- Right panel: File tree browser
  - Columns: Name, Size, Last Modified
  - Text file preview on click (< 1 MB)
  - Delete button with confirmation modal
  - Breadcrumb navigation within namespace
  - Pagination for large directories

#### Authentication Flow

1. User visits CloudFront URL → SPA loads
2. SPA checks for valid JWT in localStorage
3. If no token: redirect to login page
4. Login page calls Cognito `InitiateAuth` (USER_PASSWORD_AUTH)
5. If `NEW_PASSWORD_REQUIRED` challenge: show change-password form
6. On success: store tokens in localStorage, redirect to Dashboard
7. API calls include `Authorization: Bearer {idToken}` header (ID token, not access token — ID token's `aud` claim matches the Cognito Authorizer audience)
8. Token refresh: use refresh token before ID token expires (1 hour)
9. Logout: clear tokens from localStorage, redirect to login

Using `@aws-amplify/auth` (lightweight, Cognito-specific) for auth operations. No Hosted UI needed for this simple flow.

**Accepted risk**: JWT tokens in localStorage are accessible to any JavaScript on the page. Mitigated by: admin-only audience, no user-generated content in the SPA, dependency auditing.

### CORS Configuration

API Gateway CORS is configured with the CloudFront distribution's domain as the allowed origin. The CDK stack creates the CloudFront distribution first, then uses its domain name (`d1234abcdef.cloudfront.net`) to configure the API Gateway CORS origin. No custom domain is used — the auto-generated CloudFront domain is sufficient for admin use.

### Admin Setup Script

`scripts/setup-admin.sh`:

```bash
#!/bin/bash
# Usage: ./scripts/setup-admin.sh <email>
# Creates an admin user in the Cognito admin pool

EMAIL=$1
REGION=${CDK_DEFAULT_REGION:-us-west-2}

# Get admin user pool ID from CloudFormation
POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name OpenClawAdmin \
  --query "Stacks[0].Outputs[?OutputKey=='AdminUserPoolId'].OutputValue" \
  --output text --region $REGION)

# Generate temporary password (16 chars, mixed)
TEMP_PASSWORD=$(openssl rand -base64 16 | tr -d '/+=' | head -c 16)
# Ensure complexity: append special char + digit + uppercase
TEMP_PASSWORD="${TEMP_PASSWORD}A1!"

# Create user
aws cognito-idp admin-create-user \
  --user-pool-id $POOL_ID \
  --username "$EMAIL" \
  --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
  --temporary-password "$TEMP_PASSWORD" \
  --region $REGION

echo "Admin user created: $EMAIL"
echo "Temporary password: $TEMP_PASSWORD"
echo "Login at the CloudFront URL and change your password on first login."
```

### Frontend Deploy Script

`scripts/deploy-admin-ui.sh`:

```bash
#!/bin/bash
# Usage: ./scripts/deploy-admin-ui.sh
# Builds the admin UI and deploys to S3 + CloudFront

REGION=${CDK_DEFAULT_REGION:-us-west-2}

# Read config from CloudFormation outputs
API_URL=$(aws cloudformation describe-stacks --stack-name OpenClawAdmin \
  --query "Stacks[0].Outputs[?OutputKey=='AdminApiUrl'].OutputValue" \
  --output text --region $REGION)
POOL_ID=$(aws cloudformation describe-stacks --stack-name OpenClawAdmin \
  --query "Stacks[0].Outputs[?OutputKey=='AdminUserPoolId'].OutputValue" \
  --output text --region $REGION)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name OpenClawAdmin \
  --query "Stacks[0].Outputs[?OutputKey=='AdminClientId'].OutputValue" \
  --output text --region $REGION)
BUCKET=$(aws cloudformation describe-stacks --stack-name OpenClawAdmin \
  --query "Stacks[0].Outputs[?OutputKey=='AdminFrontendBucket'].OutputValue" \
  --output text --region $REGION)
CF_DIST_ID=$(aws cloudformation describe-stacks --stack-name OpenClawAdmin \
  --query "Stacks[0].Outputs[?OutputKey=='AdminDistributionId'].OutputValue" \
  --output text --region $REGION)

# Build with env vars
cd admin-ui
VITE_API_URL=$API_URL \
VITE_COGNITO_USER_POOL_ID=$POOL_ID \
VITE_COGNITO_CLIENT_ID=$CLIENT_ID \
VITE_COGNITO_REGION=$REGION \
npm run build

# Sync to S3
aws s3 sync dist/ s3://$BUCKET/ --delete --region $REGION

# Invalidate CloudFront cache
aws cloudfront create-invalidation --distribution-id $CF_DIST_ID --paths "/*"

echo "Admin UI deployed. CloudFront URL:"
aws cloudformation describe-stacks --stack-name OpenClawAdmin \
  --query "Stacks[0].Outputs[?OutputKey=='AdminUrl'].OutputValue" \
  --output text --region $REGION
```

### cdk.json New Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `admin_lambda_timeout_seconds` | `60` | Admin Lambda timeout |
| `admin_lambda_memory_mb` | `256` | Admin Lambda memory |

### New File Structure

```
admin-ui/                         # Frontend React SPA
  src/
    App.tsx                       # Root component, router, layout
    main.tsx                      # Entry point
    pages/
      Login.tsx                   # Login + forced password change
      Dashboard.tsx               # Stats cards + channel status
      Channels.tsx                # Channel configuration forms
      Users.tsx                   # User table + detail drawer
      Files.tsx                   # File browser
    services/
      api.ts                      # API client (fetch + JWT interceptor)
      auth.ts                     # Cognito auth (login, logout, refresh)
    components/
      ProtectedRoute.tsx          # Auth guard
      ChannelCard.tsx             # Channel config card
      UserDetail.tsx              # User detail drawer
      FileBrowser.tsx             # S3 file tree
  index.html
  package.json
  vite.config.ts
  tsconfig.json

stacks/
  admin_stack.py                  # New CDK stack (Phase 3)

lambda/
  admin/
    index.py                      # Admin API Lambda (single function, path routing)

scripts/
  setup-admin.sh                  # Create first admin user
  deploy-admin-ui.sh              # Build frontend + sync to S3 + invalidate CloudFront
```

## Security Considerations

1. **Admin pool isolation** — Completely separate from bot identity pool; no cross-contamination
2. **JWT validation** — API Gateway Cognito Authorizer validates every request before Lambda invocation; uses ID token with `aud` = client ID
3. **Secrets Manager access scoping** — Channel secrets (`openclaw/channels/*`): read-write. Webhook secret (`openclaw/webhook-secret`): read-only. Cannot access `openclaw/cognito-password-secret` or `openclaw/gateway-token`
4. **S3 file access** — Admin can read/delete any user's files; this is intentional for admin oversight. No write access (admin cannot inject files into user namespaces)
5. **Path traversal** — Namespace and path parameters validated with strict regex; `..` segments rejected
6. **CORS** — API Gateway CORS configured to allow only the CloudFront distribution domain (auto-generated `d*.cloudfront.net`)
7. **CloudFront + OAC** — S3 bucket not publicly accessible; only CloudFront can read via Origin Access Control
8. **SPA routing** — CloudFront custom error response (403/404 → `/index.html` with 200) handles client-side routing
9. **Forced password change** — First login requires password change; temporary password cannot be reused
10. **Audit logging** — Every mutating admin operation logged to CloudWatch with admin identity (`sub` claim). API Gateway access logging enabled
11. **cdk-nag** — Admin stack will pass AwsSolutions checks (encryption, logging, least privilege)
12. **User deletion cascade** — Deleting a user also deletes EventBridge Scheduler schedules to prevent orphaned cron jobs. S3 files are NOT auto-deleted (explicit admin action required)
13. **Accepted risks**:
    - JWT in localStorage (mitigated: admin-only, no UGC, dependency auditing)
    - DynamoDB Scan exposes BIND# records to admin Lambda (low risk: admin-only access, codes have 10-min TTL)

## Testing Strategy

### Lambda Unit Tests
```bash
cd lambda/admin && python -m pytest test_admin.py -v
```
- Channel CRUD (mock Secrets Manager)
- User CRUD + cascade deletion (mock DynamoDB + EventBridge Scheduler)
- File listing/deletion (mock S3)
- Path traversal rejection
- Stats aggregation
- Pagination (nextToken handling)
- Audit log emission

### Frontend
- Manual testing via CloudFront URL
- Component-level tests with Vitest + React Testing Library (optional, low priority)

### E2E
- Deploy stack → create admin → login → configure Telegram → add user to allowlist → verify user can message bot
