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
4. **Dashboard** — At-a-glance stats: user count, channel distribution, active sessions, channel config status
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
                                   │ JWT
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

**Dependencies**: Security (KMS CMK), Router (DynamoDB table name), AgentCore (S3 bucket name)

#### Resources

| Resource | Name | Description |
|----------|------|-------------|
| Cognito User Pool | `openclaw-admin-pool` | Admin-only, email verification, 12+ char password, optional TOTP MFA |
| Cognito App Client | `openclaw-admin-client` | USER_PASSWORD_AUTH, OAuth2 code flow, no client secret |
| Lambda Function | `openclaw-admin-api` | Python 3.12, 256 MB, 30s timeout, single function with path-based routing |
| API Gateway HTTP API | `openclaw-admin-api-gw` | Cognito JWT Authorizer on all `/api/*` routes |
| S3 Bucket | `openclaw-admin-frontend-{account}-{region}` | Static SPA assets, private (OAC only) |
| CloudFront Distribution | — | OAC to S3, SPA fallback to `index.html`, HTTPS only |

#### Lambda Environment Variables

```
IDENTITY_TABLE_NAME    = openclaw-identity
S3_USER_FILES_BUCKET   = openclaw-user-files-{account}-{region}
WEBHOOK_SECRET_ID      = openclaw/webhook-secret
TELEGRAM_SECRET_ID     = openclaw/channels/telegram
SLACK_SECRET_ID        = openclaw/channels/slack
FEISHU_SECRET_ID       = openclaw/channels/feishu
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
    - kms:Decrypt
    - kms:Encrypt
    - kms:GenerateDataKey
  Resource:
    - {CMK ARN}
```

### API Design

All endpoints require a valid Cognito JWT in the `Authorization: Bearer <token>` header. The API Gateway Cognito Authorizer validates the token before the Lambda is invoked.

#### Channel Management

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/channels` | List all channels with configuration status |
| `PUT` | `/api/channels/{channel}` | Update channel credentials in Secrets Manager |
| `DELETE` | `/api/channels/{channel}` | Reset channel credentials to empty placeholder |
| `POST` | `/api/channels/telegram/webhook` | Register Telegram webhook (calls Telegram setWebhook API) |

**Channel status detection**: Read the Secrets Manager value. If it equals the 32-character CDK-generated placeholder, the channel is "not configured". Otherwise, it is "configured".

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
3. Get API Gateway URL from `API_URL` environment variable (set by CDK from Router stack output)
4. Call `https://api.telegram.org/bot{token}/setWebhook?url={apiUrl}webhook/telegram&secret_token={webhookSecret}`
5. Return Telegram API response

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

**GET `/api/users` implementation**:
1. Scan DynamoDB for all records where `PK` begins with `USER#` and `SK = PROFILE`
2. For each user, query `SK begins_with CHANNEL#` to get bound channels
3. Return aggregated list

**DELETE `/api/users/{userId}` cleanup sequence**:
1. Query all records under `PK = USER#{userId}` (PROFILE, CHANNEL#*, SESSION, CRON#*)
2. For each `CHANNEL#` record, delete the corresponding `CHANNEL#{channelKey} PROFILE` record
3. Delete all `USER#{userId}` records
4. Delete any `ALLOW#` records for the user's channel keys

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

**Namespace enumeration**: Use `s3:ListObjectsV2` with `Delimiter=/` to list top-level prefixes. Each prefix is a user namespace (e.g., `telegram_123456789/`).

**File content**: For text files (`.md`, `.json`, `.txt`, `.js`, etc.), return content inline. For binary files or files > 1 MB, return a presigned S3 URL (5-minute expiry).

**Path traversal prevention**: Validate that `{namespace}` matches `^[a-zA-Z0-9_-]+$` and `{path+}` contains no `..` segments.

#### Dashboard

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/stats` | Aggregate statistics |

**Response**:
```json
{
  "totalUsers": 12,
  "totalAllowlisted": 15,
  "activeSessions": 3,
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

### Frontend Design

React + Vite + Ant Design SPA. Deployed as static files to S3, served via CloudFront.

#### Page Structure

```
┌──────────────────────────────────────────────────────┐
│  OpenClaw Admin              [admin@email.com] [退出] │
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
- Stat cards: Total Users, Active Sessions, Allowlisted Users
- Channel status cards (green = configured, gray = not configured)

**3. Channels** (`/channels`)
- Three cards: Telegram, Slack, Feishu
- Each card shows: status badge, last updated time
- Click to expand configuration form:
  - **Telegram**: Bot Token input + "Register Webhook" button
  - **Slack**: Bot Token + Signing Secret inputs
  - **Feishu**: App ID + App Secret + Verification Token + Encrypt Key inputs
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

**5. Files** (`/files`)
- Left panel: Namespace list (user namespaces from S3)
- Right panel: File tree browser
  - Columns: Name, Size, Last Modified
  - Text file preview on click
  - Delete button with confirmation modal
  - Breadcrumb navigation within namespace

#### Authentication Flow

1. User visits CloudFront URL → SPA loads
2. SPA checks for valid JWT in localStorage
3. If no token: redirect to login page
4. Login page calls Cognito `InitiateAuth` (USER_PASSWORD_AUTH)
5. If `NEW_PASSWORD_REQUIRED` challenge: show change-password form
6. On success: store tokens in localStorage, redirect to Dashboard
7. API calls include `Authorization: Bearer {idToken}` header
8. Token refresh: use refresh token before ID token expires (1 hour)
9. Logout: clear tokens from localStorage, redirect to login

Using `amazon-cognito-identity-js` SDK for direct Cognito auth (no Hosted UI needed for this simple flow).

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

### cdk.json New Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `admin_lambda_timeout_seconds` | `30` | Admin Lambda timeout |
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
      api.ts                      # API client (axios + JWT interceptor)
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
  admin_stack.py                  # New CDK stack

lambda/
  admin/
    index.py                      # Admin API Lambda (single function, path routing)

scripts/
  setup-admin.sh                  # Create first admin user
  deploy-admin-ui.sh              # Build frontend + sync to S3 + invalidate CloudFront
```

## Security Considerations

1. **Admin pool isolation** — Completely separate from bot identity pool; no cross-contamination
2. **JWT validation** — API Gateway Cognito Authorizer validates every request before Lambda invocation
3. **Secrets Manager write access** — Admin Lambda can write channel tokens; scoped to `openclaw/channels/*` only (cannot access `openclaw/cognito-password-secret` or `openclaw/gateway-token`)
4. **S3 file access** — Admin can read/delete any user's files; this is intentional for admin oversight. No write access (admin cannot inject files into user namespaces)
5. **Path traversal** — Namespace and path parameters validated with strict regex
6. **CORS** — API Gateway CORS configured to allow only the CloudFront domain
7. **CloudFront + OAC** — S3 bucket not publicly accessible; only CloudFront can read via OAC
8. **Forced password change** — First login requires password change; temporary password cannot be reused
9. **cdk-nag** — Admin stack will pass AwsSolutions checks (encryption, logging, least privilege)

## Testing Strategy

### Lambda Unit Tests
```bash
cd lambda/admin && python -m pytest test_admin.py -v
```
- Channel CRUD (mock Secrets Manager)
- User CRUD (mock DynamoDB)
- File listing/deletion (mock S3)
- Path traversal rejection
- Stats aggregation

### Frontend
- Manual testing via CloudFront URL
- Component-level tests with Vitest + React Testing Library (optional, low priority)

### E2E
- Deploy stack → create admin → login → configure Telegram → add user to allowlist → verify user can message bot
