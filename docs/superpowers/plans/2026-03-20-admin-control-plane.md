# Admin Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a serverless admin control plane (React SPA + API Gateway + Lambda) for managing OpenClaw channels, users, and per-user files.

**Architecture:** React+Antd SPA on S3/CloudFront, API Gateway HTTP API with Cognito JWT Authorizer, single Python Lambda with path-based routing. Dedicated Cognito User Pool for admin auth (separate from bot pool). CDK stack in Phase 3 (after Router).

**Tech Stack:** CDK v2 (Python), Python 3.13 Lambda, React 18 + Vite + Ant Design 5, @aws-amplify/auth, TypeScript

**Spec:** `docs/design-admin-control-plane.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `stacks/admin_stack.py` | CDK stack: Cognito admin pool, API Gateway, Lambda, S3+CloudFront |
| `lambda/admin/index.py` | Admin API Lambda: route dispatch, channel/user/file/stats handlers |
| `lambda/admin/test_admin.py` | Unit tests for admin Lambda (pytest, mocked AWS) |
| `admin-ui/package.json` | Frontend dependencies |
| `admin-ui/vite.config.ts` | Vite build config |
| `admin-ui/tsconfig.json` | TypeScript config |
| `admin-ui/index.html` | SPA entry HTML |
| `admin-ui/src/main.tsx` | React entry point |
| `admin-ui/src/App.tsx` | Root component: layout, router, auth guard |
| `admin-ui/src/config.ts` | Environment variable config (API URL, Cognito IDs) |
| `admin-ui/src/services/auth.ts` | Cognito auth: login, logout, refresh, change password |
| `admin-ui/src/services/api.ts` | API client: fetch wrapper with JWT interceptor |
| `admin-ui/src/pages/Login.tsx` | Login page + forced password change |
| `admin-ui/src/pages/Dashboard.tsx` | Stats cards + channel status |
| `admin-ui/src/pages/Channels.tsx` | Channel configuration forms |
| `admin-ui/src/pages/Users.tsx` | User table + detail drawer + allowlist |
| `admin-ui/src/pages/Files.tsx` | S3 file browser |
| `scripts/setup-admin.sh` | Create first admin user in Cognito |
| `scripts/deploy-admin-ui.sh` | Build frontend + sync to S3 + invalidate CloudFront |

### Modified Files

| File | Change |
|------|--------|
| `app.py` | Add `OpenClawAdmin` stack to Phase 3 |
| `cdk.json` | Add `admin_lambda_timeout_seconds`, `admin_lambda_memory_mb` |
| `.gitignore` | Add `admin-ui/node_modules/` and `admin-ui/dist/` |

---

## Task 1: Admin Lambda — Core Framework + Stats Endpoint

**Files:**
- Create: `lambda/admin/index.py`
- Create: `lambda/admin/test_admin.py`

- [ ] **Step 1: Write test for route dispatch and stats endpoint**

Create `lambda/admin/test_admin.py`:

```python
"""Unit tests for admin API Lambda."""
import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

os.environ["IDENTITY_TABLE_NAME"] = "test-identity"
os.environ["S3_USER_FILES_BUCKET"] = "test-bucket"
os.environ["WEBHOOK_SECRET_ID"] = "openclaw/webhook-secret"
os.environ["TELEGRAM_SECRET_ID"] = "openclaw/channels/telegram"
os.environ["SLACK_SECRET_ID"] = "openclaw/channels/slack"
os.environ["FEISHU_SECRET_ID"] = "openclaw/channels/feishu"
os.environ["ROUTER_API_URL"] = "https://xxx.execute-api.us-west-2.amazonaws.com/"
os.environ["AWS_REGION"] = "us-west-2"


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear module-level caches between tests."""
    import index
    index._secret_cache.clear()
    yield


@pytest.fixture
def mock_dynamodb():
    with patch("index.identity_table") as mock_table:
        yield mock_table


@pytest.fixture
def mock_secrets():
    with patch("index._get_secret") as mock:
        yield mock


class TestRouteDispatch:
    def test_unknown_route_returns_404(self):
        from index import handler

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/api/unknown"}},
            "headers": {"authorization": "Bearer test"},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 404

    def test_get_stats(self, mock_dynamodb, mock_secrets):
        from index import handler

        # Mock DynamoDB scan for users and allowlist
        mock_dynamodb.scan.return_value = {
            "Items": [
                {"PK": "USER#user_abc", "SK": "PROFILE", "userId": "user_abc"},
                {"PK": "USER#user_abc", "SK": "CHANNEL#telegram:123", "channel": "telegram"},
                {"PK": "USER#user_def", "SK": "PROFILE", "userId": "user_def"},
                {"PK": "USER#user_def", "SK": "CHANNEL#slack:456", "channel": "slack"},
                {"PK": "ALLOW#telegram:123", "SK": "ALLOW"},
                {"PK": "ALLOW#telegram:789", "SK": "ALLOW"},
            ],
        }
        # Mock secrets for channel status
        mock_secrets.side_effect = lambda sid: (
            "real-token" if "telegram" in sid else "x" * 32
        )

        event = {
            "requestContext": {
                "http": {"method": "GET", "path": "/api/stats"},
                "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
            },
            "headers": {},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["totalUsers"] == 2
        assert body["totalAllowlisted"] == 2
        assert body["channelDistribution"]["telegram"] == 1
        assert body["channelDistribution"]["slack"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd lambda/admin && python -m pytest test_admin.py::TestRouteDispatch -v`
Expected: FAIL (no `index` module)

- [ ] **Step 3: Write the admin Lambda core**

Create `lambda/admin/index.py`:

```python
"""OpenClaw Admin API Lambda — single function, path-based routing."""
import json
import logging
import os
import time
import urllib.parse

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration ---
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
IDENTITY_TABLE_NAME = os.environ["IDENTITY_TABLE_NAME"]
S3_USER_FILES_BUCKET = os.environ["S3_USER_FILES_BUCKET"]
WEBHOOK_SECRET_ID = os.environ.get("WEBHOOK_SECRET_ID", "")
TELEGRAM_SECRET_ID = os.environ.get("TELEGRAM_SECRET_ID", "")
SLACK_SECRET_ID = os.environ.get("SLACK_SECRET_ID", "")
FEISHU_SECRET_ID = os.environ.get("FEISHU_SECRET_ID", "")
ROUTER_API_URL = os.environ.get("ROUTER_API_URL", "")

# --- AWS Clients ---
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
s3_client = boto3.client("s3", region_name=AWS_REGION)
secrets_client = boto3.client("secretsmanager", region_name=AWS_REGION)
scheduler_client = boto3.client("scheduler", region_name=AWS_REGION)

# --- Secret cache (15 min TTL) ---
_SECRET_CACHE_TTL = 900
_secret_cache = {}

CHANNEL_SECRET_IDS = {
    "telegram": TELEGRAM_SECRET_ID,
    "slack": SLACK_SECRET_ID,
    "feishu": FEISHU_SECRET_ID,
}

# Placeholder length used by CDK-generated secrets
_PLACEHOLDER_LEN = 32


def _get_secret(secret_id):
    """Fetch secret value with 15-min cache."""
    if not secret_id:
        return ""
    cached = _secret_cache.get(secret_id)
    if cached:
        val, ts = cached
        if time.time() - ts < _SECRET_CACHE_TTL:
            return val
    try:
        resp = secrets_client.get_secret_value(SecretId=secret_id)
        val = resp["SecretString"]
        _secret_cache[secret_id] = (val, time.time())
        return val
    except ClientError as e:
        logger.error("Failed to get secret %s: %s", secret_id, e)
        return ""


def _json_response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _get_admin_sub(event):
    """Extract admin Cognito sub claim from JWT authorizer context."""
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        return "unknown"


def _audit_log(admin_sub, action, target, detail=""):
    """Emit structured audit log for mutating operations."""
    logger.info(
        "AUDIT admin=%s action=%s target=%s detail=%s",
        admin_sub, action, target, detail,
    )


# ---- Stats ----

def _handle_get_stats(event):
    """GET /api/stats — aggregate dashboard statistics."""
    items = []
    params = {"FilterExpression": "begins_with(PK, :u) OR begins_with(PK, :a)",
              "ExpressionAttributeValues": {":u": "USER#", ":a": "ALLOW#"}}
    while True:
        resp = identity_table.scan(**params)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        params["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    total_users = 0
    total_allow = 0
    channel_dist = {}

    for item in items:
        pk = item.get("PK", "")
        sk = item.get("SK", "")
        if pk.startswith("USER#") and sk == "PROFILE":
            total_users += 1
        elif pk.startswith("USER#") and sk.startswith("CHANNEL#"):
            ch_key = sk.replace("CHANNEL#", "")
            ch_type = ch_key.split(":")[0] if ":" in ch_key else ch_key
            channel_dist[ch_type] = channel_dist.get(ch_type, 0) + 1
        elif pk.startswith("ALLOW#"):
            total_allow += 1

    # Channel config status
    channels = {}
    for name, sid in CHANNEL_SECRET_IDS.items():
        val = _get_secret(sid)
        channels[name] = {"configured": bool(val) and len(val) != _PLACEHOLDER_LEN}

    return _json_response(200, {
        "totalUsers": total_users,
        "totalAllowlisted": total_allow,
        "channelDistribution": channel_dist,
        "channels": channels,
    })


# ---- Route Dispatch ----

ROUTES = {}


def route(method, path):
    """Decorator to register a route handler."""
    def decorator(fn):
        ROUTES[(method, path)] = fn
        return fn
    return decorator


# Register stats route
route("GET", "/api/stats")(_handle_get_stats)


def _match_route(method, path):
    """Match request to a route handler, supporting path parameters."""
    # Exact match first
    if (method, path) in ROUTES:
        return ROUTES[(method, path)], {}

    # Pattern matching for parameterized routes
    for (route_method, route_path), handler_fn in ROUTES.items():
        if route_method != method:
            continue
        route_parts = route_path.split("/")
        path_parts = path.split("/")

        # Handle greedy {path+} parameter
        if route_parts and route_parts[-1].endswith("+}"):
            if len(path_parts) >= len(route_parts):
                params = {}
                match = True
                for i, rp in enumerate(route_parts[:-1]):
                    if rp.startswith("{") and rp.endswith("}"):
                        params[rp[1:-1]] = urllib.parse.unquote(path_parts[i])
                    elif rp != path_parts[i]:
                        match = False
                        break
                if match:
                    param_name = route_parts[-1][1:-2]  # strip { and +}
                    params[param_name] = "/".join(
                        urllib.parse.unquote(p) for p in path_parts[len(route_parts) - 1:]
                    )
                    return handler_fn, params
            continue

        if len(route_parts) != len(path_parts):
            continue
        params = {}
        match = True
        for rp, pp in zip(route_parts, path_parts):
            if rp.startswith("{") and rp.endswith("}"):
                params[rp[1:-1]] = urllib.parse.unquote(pp)
            elif rp != pp:
                match = False
                break
        if match:
            return handler_fn, params

    return None, {}


def handler(event, context):
    """Lambda entry point — route dispatch."""
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    path = http.get("path", "")

    handler_fn, path_params = _match_route(method, path)
    if not handler_fn:
        return _json_response(404, {"error": "Not found"})

    event["pathParameters"] = path_params

    # Parse query string
    qs = event.get("queryStringParameters") or {}
    event["queryParams"] = qs

    # Parse body
    body = event.get("body")
    if body:
        try:
            event["parsedBody"] = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            event["parsedBody"] = {}
    else:
        event["parsedBody"] = {}

    try:
        return handler_fn(event)
    except Exception:
        logger.exception("Unhandled error in %s %s", method, path)
        return _json_response(500, {"error": "Internal server error"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd lambda/admin && python -m pytest test_admin.py::TestRouteDispatch -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add lambda/admin/index.py lambda/admin/test_admin.py
git commit -m "feat(admin): add Lambda core framework with route dispatch and stats endpoint"
```

---

## Task 2: Admin Lambda — Channel Management Endpoints

**Files:**
- Modify: `lambda/admin/index.py`
- Modify: `lambda/admin/test_admin.py`

- [ ] **Step 1: Write tests for channel endpoints**

Append to `lambda/admin/test_admin.py`:

```python
class TestChannelManagement:
    def test_get_channels(self, mock_secrets):
        from index import handler

        mock_secrets.side_effect = lambda sid: (
            "real-bot-token" if "telegram" in sid
            else "x" * 32  # placeholder = not configured
        )

        event = {
            "requestContext": {
                "http": {"method": "GET", "path": "/api/channels"},
                "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
            },
            "headers": {},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        channels = {c["name"]: c for c in body["channels"]}
        assert channels["telegram"]["configured"] is True
        assert channels["slack"]["configured"] is False
        # All channels should have webhookUrl
        for ch in body["channels"]:
            assert "webhookUrl" in ch

    def test_put_channel_telegram(self, mock_secrets):
        from index import handler

        with patch("index.secrets_client") as mock_sm:
            event = {
                "requestContext": {
                    "http": {"method": "PUT", "path": "/api/channels/telegram"},
                    "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
                },
                "headers": {},
                "body": json.dumps({"botToken": "123456:ABC-DEF"}),
            }
            resp = handler(event, None)
            assert resp["statusCode"] == 200
            mock_sm.put_secret_value.assert_called_once()

    def test_put_channel_slack_json(self, mock_secrets):
        from index import handler

        with patch("index.secrets_client") as mock_sm:
            event = {
                "requestContext": {
                    "http": {"method": "PUT", "path": "/api/channels/slack"},
                    "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
                },
                "headers": {},
                "body": json.dumps({
                    "botToken": "xoxb-123",
                    "signingSecret": "abc123",
                }),
            }
            resp = handler(event, None)
            assert resp["statusCode"] == 200
            call_args = mock_sm.put_secret_value.call_args
            stored = json.loads(call_args.kwargs.get("SecretString") or call_args[1]["SecretString"])
            assert stored["botToken"] == "xoxb-123"
            assert stored["signingSecret"] == "abc123"

    def test_delete_channel(self, mock_secrets):
        from index import handler

        with patch("index.secrets_client") as mock_sm:
            event = {
                "requestContext": {
                    "http": {"method": "DELETE", "path": "/api/channels/telegram"},
                    "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
                },
                "headers": {},
            }
            resp = handler(event, None)
            assert resp["statusCode"] == 200
            mock_sm.put_secret_value.assert_called_once()

    def test_register_telegram_webhook(self, mock_secrets):
        from index import handler

        mock_secrets.side_effect = lambda sid: "real-token-value"

        with patch("index.urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"ok":true,"result":true}'
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            event = {
                "requestContext": {
                    "http": {"method": "POST", "path": "/api/channels/telegram/webhook"},
                    "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
                },
                "headers": {},
            }
            resp = handler(event, None)
            assert resp["statusCode"] == 200
            body = json.loads(resp["body"])
            assert body["telegramResponse"]["ok"] is True

    def test_put_unknown_channel_returns_400(self, mock_secrets):
        from index import handler

        event = {
            "requestContext": {
                "http": {"method": "PUT", "path": "/api/channels/discord"},
                "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
            },
            "headers": {},
            "body": json.dumps({"token": "abc"}),
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 400
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd lambda/admin && python -m pytest test_admin.py::TestChannelManagement -v`
Expected: FAIL (routes not registered)

- [ ] **Step 3: Implement channel endpoints**

Append to `lambda/admin/index.py` (before the handler function):

```python
# ---- Channel Management ----

SUPPORTED_CHANNELS = {"telegram", "slack", "feishu"}


@route("GET", "/api/channels")
def _handle_get_channels(event):
    """GET /api/channels — list all channels with config status and webhook URLs."""
    channels = []
    for name in SUPPORTED_CHANNELS:
        sid = CHANNEL_SECRET_IDS.get(name, "")
        val = _get_secret(sid)
        configured = bool(val) and len(val) != _PLACEHOLDER_LEN
        channels.append({
            "name": name,
            "configured": configured,
            "webhookUrl": f"{ROUTER_API_URL}webhook/{name}" if ROUTER_API_URL else "",
        })
    return _json_response(200, {"channels": channels})


@route("PUT", "/api/channels/{channel}")
def _handle_put_channel(event):
    """PUT /api/channels/{channel} — update channel credentials."""
    channel = event["pathParameters"]["channel"]
    if channel not in SUPPORTED_CHANNELS:
        return _json_response(400, {"error": f"Unknown channel: {channel}"})

    sid = CHANNEL_SECRET_IDS.get(channel)
    if not sid:
        return _json_response(400, {"error": f"No secret configured for {channel}"})

    body = event["parsedBody"]

    # Build secret value based on channel type
    if channel == "telegram":
        secret_val = body.get("botToken", "")
    elif channel == "slack":
        secret_val = json.dumps({
            "botToken": body.get("botToken", ""),
            "signingSecret": body.get("signingSecret", ""),
        })
    elif channel == "feishu":
        secret_val = json.dumps({
            "appId": body.get("appId", ""),
            "appSecret": body.get("appSecret", ""),
            "verificationToken": body.get("verificationToken", ""),
            "encryptKey": body.get("encryptKey", ""),
        })
    else:
        secret_val = json.dumps(body)

    try:
        secrets_client.put_secret_value(SecretId=sid, SecretString=secret_val)
        # Invalidate cache
        _secret_cache.pop(sid, None)
    except ClientError as e:
        logger.error("Failed to update secret for %s: %s", channel, e)
        return _json_response(500, {"error": "Failed to update credentials"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "UPDATE_CHANNEL", channel)
    return _json_response(200, {"message": f"{channel} credentials updated"})


@route("DELETE", "/api/channels/{channel}")
def _handle_delete_channel(event):
    """DELETE /api/channels/{channel} — reset credentials to placeholder."""
    channel = event["pathParameters"]["channel"]
    if channel not in SUPPORTED_CHANNELS:
        return _json_response(400, {"error": f"Unknown channel: {channel}"})

    sid = CHANNEL_SECRET_IDS.get(channel)
    if not sid:
        return _json_response(400, {"error": f"No secret configured for {channel}"})

    placeholder = "x" * _PLACEHOLDER_LEN
    try:
        secrets_client.put_secret_value(SecretId=sid, SecretString=placeholder)
        _secret_cache.pop(sid, None)
    except ClientError as e:
        logger.error("Failed to reset secret for %s: %s", channel, e)
        return _json_response(500, {"error": "Failed to reset credentials"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "RESET_CHANNEL", channel)
    return _json_response(200, {"message": f"{channel} credentials reset"})


@route("POST", "/api/channels/telegram/webhook")
def _handle_register_telegram_webhook(event):
    """POST /api/channels/telegram/webhook — register Telegram webhook."""
    token = _get_secret(TELEGRAM_SECRET_ID)
    if not token or len(token) == _PLACEHOLDER_LEN:
        return _json_response(400, {"error": "Telegram bot token not configured"})

    webhook_secret = _get_secret(WEBHOOK_SECRET_ID)
    webhook_url = f"{ROUTER_API_URL}webhook/telegram"

    import urllib.request
    url = (
        f"https://api.telegram.org/bot{token}/setWebhook"
        f"?url={urllib.parse.quote(webhook_url, safe='')}"
        f"&secret_token={urllib.parse.quote(webhook_secret, safe='')}"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        logger.error("Telegram setWebhook failed: %s", e)
        return _json_response(502, {"error": f"Telegram API error: {e}"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "REGISTER_WEBHOOK", "telegram")
    return _json_response(200, {"telegramResponse": result})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd lambda/admin && python -m pytest test_admin.py::TestChannelManagement -v`
Expected: 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add lambda/admin/index.py lambda/admin/test_admin.py
git commit -m "feat(admin): add channel management endpoints (GET/PUT/DELETE + webhook)"
```

---

## Task 3: Admin Lambda — User Management Endpoints

**Files:**
- Modify: `lambda/admin/index.py`
- Modify: `lambda/admin/test_admin.py`

- [ ] **Step 1: Write tests for user endpoints**

Append to `lambda/admin/test_admin.py`:

```python
class TestUserManagement:
    def test_get_users_paginated(self, mock_dynamodb):
        from index import handler

        mock_dynamodb.scan.return_value = {
            "Items": [
                {"PK": "USER#user_abc", "SK": "PROFILE", "userId": "user_abc",
                 "displayName": "Alice", "createdAt": "2026-01-01T00:00:00Z"},
                {"PK": "USER#user_abc", "SK": "CHANNEL#telegram:123",
                 "channel": "telegram", "channelUserId": "123"},
                {"PK": "USER#user_def", "SK": "PROFILE", "userId": "user_def",
                 "displayName": "Bob", "createdAt": "2026-02-01T00:00:00Z"},
            ],
        }

        event = {
            "requestContext": {
                "http": {"method": "GET", "path": "/api/users"},
                "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
            },
            "headers": {},
            "queryStringParameters": {"limit": "50"},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["users"]) == 2
        alice = next(u for u in body["users"] if u["userId"] == "user_abc")
        assert len(alice["channels"]) == 1
        assert alice["channels"][0]["channel"] == "telegram"

    def test_get_user_detail(self, mock_dynamodb):
        from index import handler

        mock_dynamodb.query.return_value = {
            "Items": [
                {"PK": "USER#user_abc", "SK": "PROFILE", "userId": "user_abc",
                 "displayName": "Alice", "createdAt": "2026-01-01T00:00:00Z"},
                {"PK": "USER#user_abc", "SK": "CHANNEL#telegram:123",
                 "channel": "telegram", "channelUserId": "123"},
                {"PK": "USER#user_abc", "SK": "SESSION",
                 "sessionId": "ses_abc_12345678901234567", "createdAt": "2026-03-01T00:00:00Z"},
                {"PK": "USER#user_abc", "SK": "CRON#daily_reminder",
                 "expression": "0 9 * * *", "message": "Check email",
                 "timezone": "UTC", "channel": "telegram"},
            ],
        }

        event = {
            "requestContext": {
                "http": {"method": "GET", "path": "/api/users/user_abc"},
                "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
            },
            "headers": {},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["userId"] == "user_abc"
        assert len(body["channels"]) == 1
        assert body["session"]["sessionId"] == "ses_abc_12345678901234567"
        assert len(body["cronJobs"]) == 1

    def test_delete_user_cascades(self, mock_dynamodb):
        from index import handler

        mock_dynamodb.query.return_value = {
            "Items": [
                {"PK": "USER#user_abc", "SK": "PROFILE"},
                {"PK": "USER#user_abc", "SK": "CHANNEL#telegram:123"},
                {"PK": "USER#user_abc", "SK": "SESSION"},
                {"PK": "USER#user_abc", "SK": "CRON#daily_reminder"},
            ],
        }

        with patch("index.scheduler_client") as mock_sched:
            event = {
                "requestContext": {
                    "http": {"method": "DELETE", "path": "/api/users/user_abc"},
                    "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
                },
                "headers": {},
            }
            resp = handler(event, None)
            assert resp["statusCode"] == 200
            # Verify CHANNEL# reverse record deleted
            delete_calls = mock_dynamodb.delete_item.call_args_list
            pks_deleted = [c.kwargs["Key"]["PK"] for c in delete_calls]
            assert "CHANNEL#telegram:123" in pks_deleted
            assert "ALLOW#telegram:123" in pks_deleted

    def test_post_allowlist(self, mock_dynamodb):
        from index import handler

        event = {
            "requestContext": {
                "http": {"method": "POST", "path": "/api/allowlist"},
                "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
            },
            "headers": {},
            "body": json.dumps({"channelKey": "telegram:789"}),
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 200
        mock_dynamodb.put_item.assert_called_once()
        item = mock_dynamodb.put_item.call_args.kwargs["Item"]
        assert item["PK"] == "ALLOW#telegram:789"

    def test_get_allowlist(self, mock_dynamodb):
        from index import handler

        mock_dynamodb.scan.return_value = {
            "Items": [
                {"PK": "ALLOW#telegram:123", "SK": "ALLOW",
                 "channelKey": "telegram:123", "addedAt": "2026-01-01T00:00:00Z"},
            ],
        }

        event = {
            "requestContext": {
                "http": {"method": "GET", "path": "/api/allowlist"},
                "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
            },
            "headers": {},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert len(body["entries"]) == 1

    def test_delete_allowlist(self, mock_dynamodb):
        from index import handler

        event = {
            "requestContext": {
                "http": {"method": "DELETE",
                         "path": "/api/allowlist/telegram%3A789"},
                "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
            },
            "headers": {},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 200
        mock_dynamodb.delete_item.assert_called_once_with(
            Key={"PK": "ALLOW#telegram:789", "SK": "ALLOW"}
        )

    def test_delete_user_channel(self, mock_dynamodb):
        from index import handler

        event = {
            "requestContext": {
                "http": {"method": "DELETE",
                         "path": "/api/users/user_abc/channels/telegram%3A123"},
                "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
            },
            "headers": {},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 200
        delete_calls = mock_dynamodb.delete_item.call_args_list
        assert len(delete_calls) >= 2  # USER# CHANNEL# + CHANNEL# PROFILE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd lambda/admin && python -m pytest test_admin.py::TestUserManagement -v`
Expected: FAIL (routes not registered)

- [ ] **Step 3: Implement user management endpoints**

Append to `lambda/admin/index.py` (after channel management, before handler):

```python
# ---- User Management ----

@route("GET", "/api/users")
def _handle_get_users(event):
    """GET /api/users — list all users with bound channels."""
    qs = event.get("queryParams", {})
    limit = min(int(qs.get("limit", "50")), 200)
    next_token = qs.get("nextToken")

    params = {
        "FilterExpression": "begins_with(PK, :u)",
        "ExpressionAttributeValues": {":u": "USER#"},
        "Limit": limit * 5,  # Over-fetch since filter is post-scan
    }
    if next_token:
        params["ExclusiveStartKey"] = json.loads(
            urllib.parse.unquote(next_token)
        )

    items = []
    resp = identity_table.scan(**params)
    items.extend(resp.get("Items", []))
    result_next = resp.get("LastEvaluatedKey")

    # Group by userId
    users_map = {}
    for item in items:
        pk = item.get("PK", "")
        sk = item.get("SK", "")
        if not pk.startswith("USER#"):
            continue
        user_id = pk.replace("USER#", "")
        if user_id not in users_map:
            users_map[user_id] = {"userId": user_id, "channels": []}

        if sk == "PROFILE":
            users_map[user_id]["displayName"] = item.get("displayName", "")
            users_map[user_id]["createdAt"] = item.get("createdAt", "")
        elif sk.startswith("CHANNEL#"):
            users_map[user_id]["channels"].append({
                "channelKey": sk.replace("CHANNEL#", ""),
                "channel": item.get("channel", ""),
                "channelUserId": item.get("channelUserId", ""),
            })

    users = sorted(users_map.values(), key=lambda u: u.get("createdAt", ""), reverse=True)

    result = {"users": users[:limit]}
    if result_next:
        result["nextToken"] = urllib.parse.quote(json.dumps(result_next, default=str))
    return _json_response(200, result)


@route("GET", "/api/users/{userId}")
def _handle_get_user(event):
    """GET /api/users/{userId} — user detail."""
    user_id = event["pathParameters"]["userId"]

    resp = identity_table.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": f"USER#{user_id}"},
    )
    items = resp.get("Items", [])
    if not items:
        return _json_response(404, {"error": "User not found"})

    profile = {}
    channels = []
    session = None
    cron_jobs = []

    for item in items:
        sk = item.get("SK", "")
        if sk == "PROFILE":
            profile = {
                "userId": item.get("userId", ""),
                "displayName": item.get("displayName", ""),
                "createdAt": item.get("createdAt", ""),
            }
        elif sk.startswith("CHANNEL#"):
            channels.append({
                "channelKey": sk.replace("CHANNEL#", ""),
                "channel": item.get("channel", ""),
                "channelUserId": item.get("channelUserId", ""),
                "boundAt": item.get("boundAt", ""),
            })
        elif sk == "SESSION":
            session = {
                "sessionId": item.get("sessionId", ""),
                "createdAt": item.get("createdAt", ""),
                "lastActivity": item.get("lastActivity", ""),
            }
        elif sk.startswith("CRON#"):
            cron_jobs.append({
                "name": sk.replace("CRON#", ""),
                "expression": item.get("expression", ""),
                "message": item.get("message", ""),
                "timezone": item.get("timezone", ""),
                "channel": item.get("channel", ""),
            })

    return _json_response(200, {
        **profile,
        "channels": channels,
        "session": session,
        "cronJobs": cron_jobs,
    })


@route("DELETE", "/api/users/{userId}")
def _handle_delete_user(event):
    """DELETE /api/users/{userId} — delete user and cascade."""
    user_id = event["pathParameters"]["userId"]
    admin_sub = _get_admin_sub(event)

    resp = identity_table.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": f"USER#{user_id}"},
    )
    items = resp.get("Items", [])
    if not items:
        return _json_response(404, {"error": "User not found"})

    channel_keys = []
    for item in items:
        sk = item.get("SK", "")

        # Delete CHANNEL# reverse mapping
        if sk.startswith("CHANNEL#"):
            ch_key = sk.replace("CHANNEL#", "")
            channel_keys.append(ch_key)
            try:
                identity_table.delete_item(Key={"PK": f"CHANNEL#{ch_key}", "SK": "PROFILE"})
            except ClientError as e:
                logger.error("Failed to delete CHANNEL# record: %s", e)

        # Delete EventBridge schedules for CRON# records
        if sk.startswith("CRON#"):
            schedule_name = sk.replace("CRON#", "")
            try:
                scheduler_client.delete_schedule(
                    Name=schedule_name, GroupName="openclaw-cron",
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ResourceNotFoundException":
                    logger.error("Failed to delete schedule %s: %s", schedule_name, e)

        # Delete the USER# record itself
        try:
            identity_table.delete_item(Key={"PK": f"USER#{user_id}", "SK": sk})
        except ClientError as e:
            logger.error("Failed to delete USER# record %s: %s", sk, e)

    # Delete ALLOW# records for all channel keys
    for ch_key in channel_keys:
        try:
            identity_table.delete_item(Key={"PK": f"ALLOW#{ch_key}", "SK": "ALLOW"})
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                logger.error("Failed to delete ALLOW# for %s: %s", ch_key, e)

    _audit_log(admin_sub, "DELETE_USER", user_id,
               f"channels={channel_keys}")
    return _json_response(200, {"message": f"User {user_id} deleted"})


@route("DELETE", "/api/users/{userId}/channels/{channelKey}")
def _handle_delete_user_channel(event):
    """DELETE /api/users/{userId}/channels/{channelKey} — unbind a channel."""
    user_id = event["pathParameters"]["userId"]
    channel_key = event["pathParameters"]["channelKey"]
    admin_sub = _get_admin_sub(event)

    # Delete USER# CHANNEL# back-reference
    try:
        identity_table.delete_item(
            Key={"PK": f"USER#{user_id}", "SK": f"CHANNEL#{channel_key}"}
        )
    except ClientError as e:
        logger.error("Failed to delete user channel: %s", e)

    # Delete CHANNEL# PROFILE mapping
    try:
        identity_table.delete_item(
            Key={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE"}
        )
    except ClientError as e:
        logger.error("Failed to delete channel profile: %s", e)

    _audit_log(admin_sub, "UNBIND_CHANNEL", f"{user_id}/{channel_key}")
    return _json_response(200, {"message": f"Channel {channel_key} unbound from {user_id}"})


# ---- Allowlist ----

@route("GET", "/api/allowlist")
def _handle_get_allowlist(event):
    """GET /api/allowlist — list all allowlist entries."""
    qs = event.get("queryParams", {})
    limit = min(int(qs.get("limit", "50")), 200)
    next_token = qs.get("nextToken")

    params = {
        "FilterExpression": "begins_with(PK, :a)",
        "ExpressionAttributeValues": {":a": "ALLOW#"},
        "Limit": limit * 2,
    }
    if next_token:
        params["ExclusiveStartKey"] = json.loads(
            urllib.parse.unquote(next_token)
        )

    resp = identity_table.scan(**params)
    entries = []
    for item in resp.get("Items", []):
        entries.append({
            "channelKey": item.get("channelKey", item.get("PK", "").replace("ALLOW#", "")),
            "addedAt": item.get("addedAt", ""),
        })

    result = {"entries": entries[:limit]}
    if resp.get("LastEvaluatedKey"):
        result["nextToken"] = urllib.parse.quote(
            json.dumps(resp["LastEvaluatedKey"], default=str)
        )
    return _json_response(200, result)


@route("POST", "/api/allowlist")
def _handle_post_allowlist(event):
    """POST /api/allowlist — add allowlist entry."""
    body = event["parsedBody"]
    channel_key = body.get("channelKey", "").strip()
    if not channel_key or ":" not in channel_key:
        return _json_response(400, {"error": "channelKey must be in format 'channel:id'"})

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        identity_table.put_item(Item={
            "PK": f"ALLOW#{channel_key}",
            "SK": "ALLOW",
            "channelKey": channel_key,
            "addedAt": now_iso,
        })
    except ClientError as e:
        logger.error("Failed to add allowlist entry: %s", e)
        return _json_response(500, {"error": "Failed to add allowlist entry"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "ADD_ALLOWLIST", channel_key)
    return _json_response(200, {"message": f"Added {channel_key} to allowlist"})


@route("DELETE", "/api/allowlist/{channelKey}")
def _handle_delete_allowlist(event):
    """DELETE /api/allowlist/{channelKey} — remove allowlist entry."""
    channel_key = event["pathParameters"]["channelKey"]
    admin_sub = _get_admin_sub(event)

    try:
        identity_table.delete_item(Key={"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"})
    except ClientError as e:
        logger.error("Failed to delete allowlist entry: %s", e)
        return _json_response(500, {"error": "Failed to delete allowlist entry"})

    _audit_log(admin_sub, "REMOVE_ALLOWLIST", channel_key)
    return _json_response(200, {"message": f"Removed {channel_key} from allowlist"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd lambda/admin && python -m pytest test_admin.py::TestUserManagement -v`
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add lambda/admin/index.py lambda/admin/test_admin.py
git commit -m "feat(admin): add user management and allowlist endpoints"
```

---

## Task 4: Admin Lambda — File Management Endpoints

**Files:**
- Modify: `lambda/admin/index.py`
- Modify: `lambda/admin/test_admin.py`

- [ ] **Step 1: Write tests for file endpoints**

Append to `lambda/admin/test_admin.py`:

```python
class TestFileManagement:
    def test_list_namespaces(self):
        from index import handler

        with patch("index.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = {
                "CommonPrefixes": [
                    {"Prefix": "telegram_123/"},
                    {"Prefix": "slack_456/"},
                ],
            }
            event = {
                "requestContext": {
                    "http": {"method": "GET", "path": "/api/files"},
                    "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
                },
                "headers": {},
            }
            resp = handler(event, None)
            assert resp["statusCode"] == 200
            body = json.loads(resp["body"])
            assert len(body["namespaces"]) == 2
            assert body["namespaces"][0] == "telegram_123"

    def test_list_files_in_namespace(self):
        from index import handler

        with patch("index.s3_client") as mock_s3:
            mock_s3.list_objects_v2.return_value = {
                "Contents": [
                    {"Key": "telegram_123/.openclaw/config.json", "Size": 256,
                     "LastModified": "2026-03-01T00:00:00Z"},
                    {"Key": "telegram_123/notes.md", "Size": 1024,
                     "LastModified": "2026-03-15T00:00:00Z"},
                ],
            }
            event = {
                "requestContext": {
                    "http": {"method": "GET", "path": "/api/files/telegram_123"},
                    "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
                },
                "headers": {},
            }
            resp = handler(event, None)
            assert resp["statusCode"] == 200
            body = json.loads(resp["body"])
            assert len(body["files"]) == 2

    def test_path_traversal_rejected(self):
        from index import handler

        event = {
            "requestContext": {
                "http": {"method": "GET", "path": "/api/files/telegram_123/../slack_456/secret"},
                "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
            },
            "headers": {},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 400

    def test_invalid_namespace_rejected(self):
        from index import handler

        event = {
            "requestContext": {
                "http": {"method": "GET", "path": "/api/files/../../etc"},
                "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
            },
            "headers": {},
        }
        resp = handler(event, None)
        assert resp["statusCode"] == 400

    def test_delete_file(self):
        from index import handler

        with patch("index.s3_client") as mock_s3:
            event = {
                "requestContext": {
                    "http": {"method": "DELETE",
                             "path": "/api/files/telegram_123/.openclaw/old-skill.json"},
                    "authorizer": {"jwt": {"claims": {"sub": "admin-1"}}},
                },
                "headers": {},
            }
            resp = handler(event, None)
            assert resp["statusCode"] == 200
            mock_s3.delete_object.assert_called_once_with(
                Bucket="test-bucket",
                Key="telegram_123/.openclaw/old-skill.json",
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd lambda/admin && python -m pytest test_admin.py::TestFileManagement -v`
Expected: FAIL

- [ ] **Step 3: Implement file management endpoints**

Append to `lambda/admin/index.py`:

```python
# ---- File Management ----

import re

_VALID_NAMESPACE = re.compile(r"^[a-zA-Z0-9_-]+$")
_TEXT_EXTENSIONS = {".md", ".json", ".txt", ".js", ".ts", ".py", ".yaml", ".yml",
                    ".toml", ".cfg", ".ini", ".sh", ".html", ".css", ".xml", ".csv"}


def _validate_namespace(ns):
    return bool(_VALID_NAMESPACE.match(ns))


def _validate_path(path):
    return ".." not in path.split("/")


@route("GET", "/api/files")
def _handle_list_namespaces(event):
    """GET /api/files — list all user namespaces."""
    qs = event.get("queryParams", {})
    continuation = qs.get("nextToken")

    params = {"Bucket": S3_USER_FILES_BUCKET, "Delimiter": "/"}
    if continuation:
        params["ContinuationToken"] = continuation

    resp = s3_client.list_objects_v2(**params)
    namespaces = [
        p["Prefix"].rstrip("/") for p in resp.get("CommonPrefixes", [])
    ]

    result = {"namespaces": namespaces}
    if resp.get("NextContinuationToken"):
        result["nextToken"] = resp["NextContinuationToken"]
    return _json_response(200, result)


@route("GET", "/api/files/{namespace}")
def _handle_list_files(event):
    """GET /api/files/{namespace} — list files in a namespace."""
    namespace = event["pathParameters"]["namespace"]
    if not _validate_namespace(namespace):
        return _json_response(400, {"error": "Invalid namespace"})

    qs = event.get("queryParams", {})
    prefix = qs.get("prefix", "")  # Optional sub-path
    continuation = qs.get("nextToken")
    limit = min(int(qs.get("limit", "100")), 1000)

    s3_prefix = f"{namespace}/{prefix}" if prefix else f"{namespace}/"
    params = {
        "Bucket": S3_USER_FILES_BUCKET,
        "Prefix": s3_prefix,
        "MaxKeys": limit,
    }
    if continuation:
        params["ContinuationToken"] = continuation

    resp = s3_client.list_objects_v2(**params)
    files = []
    for obj in resp.get("Contents", []):
        key = obj["Key"]
        rel_path = key[len(f"{namespace}/"):]  # Relative to namespace
        if not rel_path:
            continue
        files.append({
            "path": rel_path,
            "size": obj.get("Size", 0),
            "lastModified": obj.get("LastModified", ""),
        })

    result = {"files": files}
    if resp.get("NextContinuationToken"):
        result["nextToken"] = resp["NextContinuationToken"]
    return _json_response(200, result)


@route("GET", "/api/files/{namespace}/{path+}")
def _handle_get_file(event):
    """GET /api/files/{namespace}/{path+} — get file content or presigned URL."""
    namespace = event["pathParameters"]["namespace"]
    file_path = event["pathParameters"]["path"]

    if not _validate_namespace(namespace) or not _validate_path(file_path):
        return _json_response(400, {"error": "Invalid path"})

    s3_key = f"{namespace}/{file_path}"
    ext = os.path.splitext(file_path)[1].lower()

    try:
        if ext in _TEXT_EXTENSIONS:
            resp = s3_client.get_object(Bucket=S3_USER_FILES_BUCKET, Key=s3_key)
            size = resp.get("ContentLength", 0)
            if size > 1_048_576:  # 1 MB
                url = s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": S3_USER_FILES_BUCKET, "Key": s3_key},
                    ExpiresIn=300,
                )
                return _json_response(200, {"presignedUrl": url, "size": size})
            content = resp["Body"].read().decode("utf-8", errors="replace")
            return _json_response(200, {"content": content, "size": size})
        else:
            # Binary file — return presigned URL
            head = s3_client.head_object(Bucket=S3_USER_FILES_BUCKET, Key=s3_key)
            url = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": S3_USER_FILES_BUCKET, "Key": s3_key},
                ExpiresIn=300,
            )
            return _json_response(200, {
                "presignedUrl": url, "size": head.get("ContentLength", 0),
            })
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return _json_response(404, {"error": "File not found"})
        raise


@route("DELETE", "/api/files/{namespace}/{path+}")
def _handle_delete_file(event):
    """DELETE /api/files/{namespace}/{path+} — delete a file."""
    namespace = event["pathParameters"]["namespace"]
    file_path = event["pathParameters"]["path"]

    if not _validate_namespace(namespace) or not _validate_path(file_path):
        return _json_response(400, {"error": "Invalid path"})

    s3_key = f"{namespace}/{file_path}"
    try:
        s3_client.delete_object(Bucket=S3_USER_FILES_BUCKET, Key=s3_key)
    except ClientError as e:
        logger.error("Failed to delete S3 object %s: %s", s3_key, e)
        return _json_response(500, {"error": "Failed to delete file"})

    admin_sub = _get_admin_sub(event)
    _audit_log(admin_sub, "DELETE_FILE", s3_key)
    return _json_response(200, {"message": f"Deleted {file_path}"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd lambda/admin && python -m pytest test_admin.py::TestFileManagement -v`
Expected: 5 tests PASS

- [ ] **Step 5: Run ALL Lambda tests**

Run: `cd lambda/admin && python -m pytest test_admin.py -v`
Expected: ALL tests PASS

- [ ] **Step 6: Commit**

```bash
git add lambda/admin/index.py lambda/admin/test_admin.py
git commit -m "feat(admin): add file management endpoints with path traversal protection"
```

---

## Task 5: CDK Admin Stack

**Files:**
- Create: `stacks/admin_stack.py`
- Modify: `app.py`
- Modify: `cdk.json`

- [ ] **Step 1: Create the admin CDK stack**

Create `stacks/admin_stack.py`. Follow the cron_stack.py pattern for cross-stack references.

Key resources:
- Cognito User Pool (admin-only, email verification, 12+ char password, optional MFA)
- Cognito App Client (USER_PASSWORD_AUTH, no secret)
- Lambda function (Python 3.13, 256 MB, 60s, code from `lambda/admin`)
- API Gateway HTTP API with JWT Authorizer (Cognito issuer + audience)
- S3 bucket (frontend assets, private, OAC only)
- CloudFront distribution (OAC, SPA custom error response 403/404 → /index.html)
- Log groups with retention
- IAM policies per the spec
- cdk-nag suppressions
- CfnOutputs: AdminUserPoolId, AdminClientId, AdminApiUrl, AdminFrontendBucket, AdminDistributionId, AdminUrl

Refer to `docs/design-admin-control-plane.md` for exact IAM policy scoping.

- [ ] **Step 2: Wire admin stack into app.py**

Add `from stacks.admin_stack import AdminStack` import. Add `OpenClawAdmin` stack in Phase 3 (after router_stack, alongside cron_stack). Pass cross-stack values using deterministic ARNs (same pattern as cron_stack):

```python
_s3_user_files_bucket_name = f"openclaw-user-files-{_account}-{_region}"

admin_stack = AdminStack(
    app,
    "OpenClawAdmin",
    identity_table_name=_identity_table_name,
    identity_table_arn=_identity_table_arn,
    s3_user_files_bucket_name=_s3_user_files_bucket_name,
    cmk_arn=security_stack.cmk.key_arn,
    router_api_url=router_stack.http_api.url or "",
    telegram_secret_name=security_stack.channel_secrets["telegram"].secret_name,
    slack_secret_name=security_stack.channel_secrets["slack"].secret_name,
    feishu_secret_name=security_stack.channel_secrets["feishu"].secret_name,
    webhook_secret_name=security_stack.webhook_secret.secret_name,
    env=env,
)
# Note: router_api_url is a direct reference (admin depends on router, no cyclic dep).
# S3 bucket name uses deterministic string to avoid cross-stack dep on agentcore.
```

- [ ] **Step 3: Add new cdk.json parameters**

Add to `cdk.json` context:
```json
"admin_lambda_timeout_seconds": 60,
"admin_lambda_memory_mb": 256
```

- [ ] **Step 4: Run cdk synth to validate**

Run: `source .venv/bin/activate && cdk synth OpenClawAdmin 2>&1 | tail -20`
Expected: Synthesis succeeds with no cdk-nag errors

- [ ] **Step 5: Commit**

```bash
git add stacks/admin_stack.py app.py cdk.json
git commit -m "feat(admin): add OpenClawAdmin CDK stack (Cognito, API Gateway, Lambda, CloudFront)"
```

---

## Task 6: Admin Setup and Deploy Scripts

**Files:**
- Create: `scripts/setup-admin.sh`
- Create: `scripts/deploy-admin-ui.sh`

- [ ] **Step 1: Create setup-admin.sh**

Create `scripts/setup-admin.sh` per the spec. Follow `scripts/setup-telegram.sh` patterns:
- `set -euo pipefail`
- Region from `CDK_DEFAULT_REGION` with fallback
- `AWS_PROFILE` support
- CloudFormation output lookup for `AdminUserPoolId`
- `aws cognito-idp admin-create-user` with temporary password
- Print password to terminal
- Input validation (email format)

- [ ] **Step 2: Create deploy-admin-ui.sh**

Create `scripts/deploy-admin-ui.sh` per the spec. Key steps:
- Read API URL, Pool ID, Client ID, bucket, distribution ID from CloudFormation outputs
- Build frontend with Vite env vars (`VITE_API_URL`, `VITE_COGNITO_*`)
- `aws s3 sync dist/ s3://$BUCKET/ --delete`
- `aws cloudfront create-invalidation --distribution-id $CF_DIST_ID --paths "/*"`
- Print CloudFront URL

- [ ] **Step 3: Make scripts executable**

Run: `chmod +x scripts/setup-admin.sh scripts/deploy-admin-ui.sh`

- [ ] **Step 4: Commit**

```bash
git add scripts/setup-admin.sh scripts/deploy-admin-ui.sh
git commit -m "feat(admin): add setup-admin.sh and deploy-admin-ui.sh scripts"
```

---

## Task 7: Frontend — Project Setup + Auth

**Files:**
- Create: `admin-ui/package.json`, `admin-ui/vite.config.ts`, `admin-ui/tsconfig.json`, `admin-ui/index.html`
- Create: `admin-ui/src/main.tsx`, `admin-ui/src/App.tsx`, `admin-ui/src/config.ts`
- Create: `admin-ui/src/services/auth.ts`, `admin-ui/src/services/api.ts`
- Create: `admin-ui/src/pages/Login.tsx`

- [ ] **Step 1: Initialize frontend project**

Create `admin-ui/package.json` with dependencies:
- `react`, `react-dom`, `react-router-dom`
- `antd`, `@ant-design/icons`
- `@aws-amplify/auth`, `@aws-amplify/core`
- Dev: `vite`, `@vitejs/plugin-react`, `typescript`, `@types/react`, `@types/react-dom`

- [ ] **Step 2: Create build config files**

Create `admin-ui/vite.config.ts`, `admin-ui/tsconfig.json`, `admin-ui/index.html`.

- [ ] **Step 3: Create config and auth service**

Create `admin-ui/src/config.ts`:
- Read `VITE_API_URL`, `VITE_COGNITO_USER_POOL_ID`, `VITE_COGNITO_CLIENT_ID`, `VITE_COGNITO_REGION`

Create `admin-ui/src/services/auth.ts`:
- Configure Amplify with Cognito settings
- `signIn(email, password)` — calls Cognito InitiateAuth
- `completeNewPassword(newPassword)` — handles NEW_PASSWORD_REQUIRED challenge
- `signOut()` — clears session
- `getToken()` — returns current ID token (with auto-refresh)
- `isAuthenticated()` — checks for valid session

- [ ] **Step 4: Create API service**

Create `admin-ui/src/services/api.ts`:
- `apiClient` — fetch wrapper that adds `Authorization: Bearer {idToken}` header
- `get(path)`, `post(path, body)`, `put(path, body)`, `del(path)` helpers
- Auto-redirect to login on 401

- [ ] **Step 5: Create Login page**

Create `admin-ui/src/pages/Login.tsx`:
- Ant Design `Form` with email + password inputs
- Handle `NEW_PASSWORD_REQUIRED` challenge (show new password form)
- Error display
- Redirect to `/` on success

- [ ] **Step 6: Create App root with routing**

Create `admin-ui/src/main.tsx` and `admin-ui/src/App.tsx`:
- Ant Design `Layout` with `Sider` (menu) + `Content`
- `react-router-dom` routes: `/login`, `/`, `/channels`, `/users`, `/files`
- `ProtectedRoute` component that checks auth
- Header with admin email + logout button
- Menu items: Dashboard, Channels, Users, Files

- [ ] **Step 7: Install deps and verify build**

Run: `cd admin-ui && npm install && npm run build`
Expected: Build succeeds, `dist/` directory created

- [ ] **Step 8: Commit**

```bash
git add admin-ui/
git commit -m "feat(admin-ui): project setup, auth service, login page, app shell"
```

---

## Task 8: Frontend — Dashboard Page

**Files:**
- Create: `admin-ui/src/pages/Dashboard.tsx`

- [ ] **Step 1: Create Dashboard page**

Dashboard.tsx:
- Call `GET /api/stats` on mount
- Ant Design `Statistic` cards: Total Users, Allowlisted Users
- Channel distribution display (tag counts)
- Channel status cards: green `CheckCircle` for configured, gray `CloseCircle` for not configured
- Loading spinner while fetching

- [ ] **Step 2: Verify build**

Run: `cd admin-ui && npm run build`
Expected: Build succeeds

- [ ] **Step 3: Commit**

```bash
git add admin-ui/src/pages/Dashboard.tsx
git commit -m "feat(admin-ui): add Dashboard page with stats and channel status"
```

---

## Task 9: Frontend — Channels Page

**Files:**
- Create: `admin-ui/src/pages/Channels.tsx`

- [ ] **Step 1: Create Channels page**

Channels.tsx:
- Call `GET /api/channels` on mount
- Three `Card` components (Telegram, Slack, Feishu)
- Each card: status badge, webhook URL with copy button
- Expand to show config form:
  - Telegram: Bot Token `Input.Password` + "Register Webhook" `Button`
  - Slack: Bot Token + Signing Secret inputs
  - Feishu: 4 fields (appId, appSecret, verificationToken, encryptKey)
- Save calls `PUT /api/channels/{channel}`
- Register Webhook calls `POST /api/channels/telegram/webhook`
- Clear calls `DELETE /api/channels/{channel}` with confirm modal
- Success/error notifications

- [ ] **Step 2: Verify build**

Run: `cd admin-ui && npm run build`
Expected: Build succeeds

- [ ] **Step 3: Commit**

```bash
git add admin-ui/src/pages/Channels.tsx
git commit -m "feat(admin-ui): add Channels page with config forms and webhook registration"
```

---

## Task 10: Frontend — Users Page

**Files:**
- Create: `admin-ui/src/pages/Users.tsx`

- [ ] **Step 1: Create Users page**

Users.tsx:
- Call `GET /api/users` on mount
- Ant Design `Table` with columns: User ID, Display Name, Channels (Tag list), Created At, Actions
- Actions column: "Detail" button opens `Drawer`, "Delete" with `Popconfirm`
- User detail `Drawer`:
  - Profile section
  - Channels list with "Unbind" button per channel (DELETE `/api/users/{id}/channels/{key}`)
  - Session info (if exists)
  - Cron jobs table
- "Add to Allowlist" button → `Modal` with `Input` for channel key (e.g., `telegram:123456`)
  - Calls `POST /api/allowlist`
- Allowlist tab/section: `Table` of allowlist entries with delete button
  - Calls `GET /api/allowlist`, `DELETE /api/allowlist/{key}`
- Pagination controls (nextToken)
- Search input (client-side filter on userId/displayName)

- [ ] **Step 2: Verify build**

Run: `cd admin-ui && npm run build`
Expected: Build succeeds

- [ ] **Step 3: Commit**

```bash
git add admin-ui/src/pages/Users.tsx
git commit -m "feat(admin-ui): add Users page with table, detail drawer, and allowlist management"
```

---

## Task 11: Frontend — Files Page

**Files:**
- Create: `admin-ui/src/pages/Files.tsx`

- [ ] **Step 1: Create Files page**

Files.tsx:
- Left panel: Call `GET /api/files`, display namespace list as `Menu` items
- Click namespace → right panel shows file list from `GET /api/files/{namespace}`
- Ant Design `Table`: Path, Size (human-readable), Last Modified
- Click text file → `Modal` with content preview from `GET /api/files/{ns}/{path}`
- Click binary file → open presigned URL in new tab
- Delete button with `Popconfirm` → `DELETE /api/files/{ns}/{path}`
- Breadcrumb navigation
- Pagination for large directories

- [ ] **Step 2: Verify full build**

Run: `cd admin-ui && npm run build`
Expected: Build succeeds, all pages compile

- [ ] **Step 3: Commit**

```bash
git add admin-ui/src/pages/Files.tsx
git commit -m "feat(admin-ui): add Files page with S3 browser and file preview"
```

---

## Task 12: Integration Test + Documentation

**Files:**
- Modify: `docs/design-admin-control-plane.md` (status → Final)
- Modify: `CLAUDE.md` (add admin stack to project structure)

- [ ] **Step 1: Run all Lambda tests**

Run: `cd lambda/admin && python -m pytest test_admin.py -v`
Expected: ALL tests PASS

- [ ] **Step 2: Run CDK synth**

Run: `source .venv/bin/activate && cdk synth 2>&1 | tail -5`
Expected: All stacks synthesize with no cdk-nag errors

- [ ] **Step 3: Verify frontend builds**

Run: `cd admin-ui && npm run build`
Expected: Build succeeds

- [ ] **Step 4: Update CLAUDE.md**

Add `OpenClawAdmin` to the CDK stacks table and project structure section. Add admin-specific commands to Expected Commands section.

- [ ] **Step 5: Update design doc status**

Change status from "Draft" to "Implemented" in `docs/design-admin-control-plane.md`.

- [ ] **Step 6: Final commit**

```bash
git add -A
git commit -m "docs: update CLAUDE.md and design doc for admin control plane"
```
