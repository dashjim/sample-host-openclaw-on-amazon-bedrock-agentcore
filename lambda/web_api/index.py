"""Web API Lambda — HTTP bootstrap for Web UI channel.

Handles session creation and pre-signed WebSocket URL generation.
Browser calls POST /api/session with Cognito JWT → Lambda returns
{sessionId, wsUrl} → browser connects directly via WebSocket.

Path routing:
  POST /api/session  — Create/resume session, return pre-signed WSS URL
  GET  /api/session  — Check session status (lightweight health check)
  POST /api/link     — Cross-channel binding (link web account to Telegram/Slack)
"""

import hashlib
import json
import logging
import os
import time
import uuid
from urllib.parse import urlparse, urlencode, quote

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration ---
AGENTCORE_RUNTIME_ARN = os.environ["AGENTCORE_RUNTIME_ARN"]
AGENTCORE_QUALIFIER = os.environ.get("AGENTCORE_QUALIFIER", "DEFAULT")
IDENTITY_TABLE_NAME = os.environ["IDENTITY_TABLE_NAME"]
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
REGISTRATION_OPEN = os.environ.get("REGISTRATION_OPEN", "false").lower() == "true"
PRESIGNED_URL_EXPIRES = int(os.environ.get("PRESIGNED_URL_EXPIRES", "300"))

# --- Clients ---
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
agentcore_client = boto3.client(
    "bedrock-agentcore",
    region_name=AWS_REGION,
    config=Config(read_timeout=60, connect_timeout=10, retries={"max_attempts": 0}),
)
_session = boto3.Session()


# ---------------------------------------------------------------------------
# Pre-signed WebSocket URL generation (pure botocore, no external SDK)
# ---------------------------------------------------------------------------

def generate_presigned_ws_url(runtime_arn, session_id, expires=300):
    """Generate a SigV4 pre-signed WSS URL for AgentCore WebSocket.

    Uses botocore's SigV4QueryAuth — same mechanism as the bedrock-agentcore
    SDK but with zero extra dependencies (botocore ships with Lambda runtime).
    """
    encoded_arn = quote(runtime_arn, safe="")
    base_url = f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com/runtimes/{encoded_arn}/ws"

    params = {
        "qualifier": AGENTCORE_QUALIFIER,
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    url_with_params = f"{base_url}?{urlencode(params)}"

    credentials = _session.get_credentials().get_frozen_credentials()
    request = AWSRequest(
        method="GET",
        url=url_with_params,
        headers={"host": f"bedrock-agentcore.{AWS_REGION}.amazonaws.com"},
    )
    SigV4QueryAuth(credentials, "bedrock-agentcore", AWS_REGION, expires=expires).add_auth(request)

    return request.url.replace("https://", "wss://")


# ---------------------------------------------------------------------------
# DynamoDB identity helpers (reused from router/index.py)
# ---------------------------------------------------------------------------

def is_user_allowed(channel, channel_user_id):
    if REGISTRATION_OPEN:
        return True
    channel_key = f"{channel}:{channel_user_id}"
    try:
        resp = identity_table.get_item(Key={"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"})
        return "Item" in resp
    except ClientError as e:
        logger.error("Allowlist check failed: %s", e)
        return False


def resolve_user(channel, channel_user_id, display_name=""):
    """Look up or create a user. Returns (user_id, is_new) or (None, False)."""
    channel_key = f"{channel}:{channel_user_id}"
    pk = f"CHANNEL#{channel_key}"

    try:
        resp = identity_table.get_item(Key={"PK": pk, "SK": "PROFILE"})
        if "Item" in resp:
            return resp["Item"]["userId"], False
    except ClientError as e:
        logger.error("DynamoDB lookup failed: %s", e)
        return None, False

    if not is_user_allowed(channel, channel_user_id):
        logger.warning("User %s not on allowlist", channel_key)
        return None, False

    user_id = f"user_{hashlib.sha256(channel_key.encode()).hexdigest()[:16]}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        identity_table.put_item(
            Item={"PK": f"USER#{user_id}", "SK": "PROFILE",
                  "userId": user_id, "createdAt": now_iso,
                  "displayName": display_name or channel_user_id},
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError:
        pass

    try:
        identity_table.put_item(
            Item={"PK": pk, "SK": "PROFILE",
                  "userId": user_id, "channel": channel,
                  "channelUserId": channel_user_id,
                  "displayName": display_name or channel_user_id,
                  "boundAt": now_iso},
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            resp = identity_table.get_item(Key={"PK": pk, "SK": "PROFILE"})
            if "Item" in resp:
                return resp["Item"]["userId"], False

    try:
        identity_table.put_item(
            Item={"PK": f"USER#{user_id}", "SK": f"CHANNEL#{channel_key}",
                  "channel": channel, "channelUserId": channel_user_id,
                  "boundAt": now_iso},
        )
    except ClientError:
        pass

    logger.info("New user: %s for %s", user_id, channel_key)
    return user_id, True


def get_or_create_session(user_id):
    """Get or create session. Returns session_id (>= 33 chars)."""
    pk = f"USER#{user_id}"

    try:
        resp = identity_table.get_item(Key={"PK": pk, "SK": "SESSION"})
        if "Item" in resp:
            identity_table.update_item(
                Key={"PK": pk, "SK": "SESSION"},
                UpdateExpression="SET lastActivity = :now",
                ExpressionAttributeValues={":now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            )
            return resp["Item"]["sessionId"]
    except ClientError as e:
        logger.error("Session lookup failed: %s", e)

    session_id = f"ses_{user_id}_{uuid.uuid4().hex[:12]}"
    if len(session_id) < 33:
        session_id += "_" + uuid.uuid4().hex[:33 - len(session_id)]
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    try:
        identity_table.put_item(
            Item={"PK": pk, "SK": "SESSION", "sessionId": session_id,
                  "createdAt": now_iso, "lastActivity": now_iso},
        )
    except ClientError as e:
        logger.error("Failed to create session: %s", e)

    logger.info("New session: %s for %s", session_id, user_id)
    return session_id


# ---------------------------------------------------------------------------
# AgentCore warmup
# ---------------------------------------------------------------------------

def warmup_container(session_id, user_id, actor_id):
    """Send warmup action to ensure container is alive. Returns status string."""
    try:
        resp = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            qualifier=AGENTCORE_QUALIFIER,
            runtimeSessionId=session_id,
            runtimeUserId=actor_id,
            payload=json.dumps({
                "action": "warmup",
                "userId": user_id,
                "actorId": actor_id,
                "channel": "web",
            }).encode(),
            contentType="application/json",
            accept="application/json",
        )
        body = resp.get("response")
        if body and hasattr(body, "read"):
            body = body.read().decode("utf-8", errors="replace")
        result = json.loads(body) if body else {}
        return result.get("status", "unknown")
    except Exception as e:
        logger.error("Warmup failed: %s", e, exc_info=True)
        return "error"


# ---------------------------------------------------------------------------
# API Handlers
# ---------------------------------------------------------------------------

def _cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Authorization,Content-Type",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
    }


def _json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {**_cors_headers(), "Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _get_jwt_claims(event):
    """Extract JWT claims from API Gateway v2 authorizer context."""
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]
    except (KeyError, TypeError):
        return None


def handle_create_session(event):
    """POST /api/session — Bootstrap: resolve user → warmup → return wsUrl."""
    claims = _get_jwt_claims(event)
    if not claims:
        return _json_response(401, {"error": "Missing or invalid JWT"})

    cognito_sub = claims.get("sub", "")
    email = claims.get("email", "")
    if not cognito_sub:
        return _json_response(400, {"error": "JWT missing 'sub' claim"})

    actor_id = f"web:{cognito_sub}"
    logger.info("Session request: actor=%s email=%s", actor_id, email)

    user_id, is_new = resolve_user("web", cognito_sub, display_name=email)
    if user_id is None:
        return _json_response(403, {
            "error": "User not allowed",
            "message": f"Your ID: web:{cognito_sub}. Send this to the admin to request access.",
        })

    session_id = get_or_create_session(user_id)

    status = warmup_container(session_id, user_id, actor_id)
    logger.info("Warmup status: %s (session=%s)", status, session_id)

    ws_url = generate_presigned_ws_url(
        AGENTCORE_RUNTIME_ARN, session_id, expires=PRESIGNED_URL_EXPIRES,
    )

    return _json_response(200, {
        "sessionId": session_id,
        "wsUrl": ws_url,
        "wsExpires": PRESIGNED_URL_EXPIRES,
        "status": status,
        "userId": user_id,
        "isNew": is_new,
    })


def handle_get_session(event):
    """GET /api/session — Check if user has an active session."""
    claims = _get_jwt_claims(event)
    if not claims:
        return _json_response(401, {"error": "Missing or invalid JWT"})

    cognito_sub = claims.get("sub", "")
    channel_key = f"web:{cognito_sub}"
    pk = f"CHANNEL#{channel_key}"

    try:
        resp = identity_table.get_item(Key={"PK": pk, "SK": "PROFILE"})
        if "Item" not in resp:
            return _json_response(404, {"error": "User not found"})

        user_id = resp["Item"]["userId"]
        sess_resp = identity_table.get_item(Key={"PK": f"USER#{user_id}", "SK": "SESSION"})
        if "Item" not in sess_resp:
            return _json_response(404, {"error": "No active session"})

        session_id = sess_resp["Item"]["sessionId"]
        ws_url = generate_presigned_ws_url(
            AGENTCORE_RUNTIME_ARN, session_id, expires=PRESIGNED_URL_EXPIRES,
        )

        return _json_response(200, {
            "sessionId": session_id,
            "wsUrl": ws_url,
            "wsExpires": PRESIGNED_URL_EXPIRES,
            "userId": user_id,
        })
    except ClientError as e:
        logger.error("Session check failed: %s", e)
        return _json_response(500, {"error": "Internal error"})


def handle_link_channel(event):
    """POST /api/link — Generate cross-channel bind code."""
    claims = _get_jwt_claims(event)
    if not claims:
        return _json_response(401, {"error": "Missing or invalid JWT"})

    cognito_sub = claims.get("sub", "")
    channel_key = f"web:{cognito_sub}"
    pk = f"CHANNEL#{channel_key}"

    try:
        resp = identity_table.get_item(Key={"PK": pk, "SK": "PROFILE"})
        if "Item" not in resp:
            return _json_response(404, {"error": "User not found — create session first"})

        user_id = resp["Item"]["userId"]
    except ClientError as e:
        logger.error("User lookup failed: %s", e)
        return _json_response(500, {"error": "Internal error"})

    bind_code = uuid.uuid4().hex[:6].upper()
    ttl = int(time.time()) + 600  # 10 minutes

    try:
        identity_table.put_item(
            Item={
                "PK": f"BIND#{bind_code}",
                "SK": "BIND",
                "userId": user_id,
                "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "ttl": ttl,
            }
        )
    except ClientError as e:
        logger.error("Bind code creation failed: %s", e)
        return _json_response(500, {"error": "Internal error"})

    return _json_response(200, {
        "bindCode": bind_code,
        "expiresIn": 600,
        "instructions": f"Send this code to the bot on Telegram/Slack/Feishu to link your accounts: {bind_code}",
    })


# ---------------------------------------------------------------------------
# Lambda Handler
# ---------------------------------------------------------------------------

def handler(event, context):
    """API Gateway HTTP API v2 handler."""
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")

    logger.info("Request: %s %s", method, path)

    if method == "OPTIONS":
        return _json_response(204, "")

    if path == "/api/session":
        if method == "POST":
            return handle_create_session(event)
        if method == "GET":
            return handle_get_session(event)

    if path == "/api/link" and method == "POST":
        return handle_link_channel(event)

    return _json_response(404, {"error": f"Not found: {method} {path}"})
