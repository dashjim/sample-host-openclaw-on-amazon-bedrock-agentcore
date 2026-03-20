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
