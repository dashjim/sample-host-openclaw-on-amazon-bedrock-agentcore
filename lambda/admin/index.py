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
