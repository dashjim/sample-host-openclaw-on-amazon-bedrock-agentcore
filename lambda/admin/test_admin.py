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
