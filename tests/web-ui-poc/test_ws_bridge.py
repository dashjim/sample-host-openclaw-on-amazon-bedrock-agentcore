#!/usr/bin/env python3
"""
WebSocket bridge POC test — verifies browser can reach OpenClaw Gateway Protocol
via AgentCore platform auto-bridge.

AgentCore platform auto-discovers the container's OpenClaw Gateway (port 18789)
and bridges WSS connections directly. No /ws handler on 8080 needed.

Usage:
    source ~/ws-poc-env.sh   # optional — sets env vars
    python3 test_ws_bridge.py

Requires: bedrock-agentcore SDK, websockets, boto3
"""

import asyncio
import json
import os
import sys
import time

import websockets

RUNTIME_ARN = os.environ.get(
    "RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-west-2:576186206185:runtime/openclaw_agent-FMElB5ECU7",
)
SESSION_ID = os.environ.get(
    "SESSION_ID", "ses_user_c1874612116a454b_69185b4354bc"
)
REGION = os.environ.get("AWS_REGION", "us-west-2")
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")

req_counter = 0


def next_id():
    global req_counter
    req_counter += 1
    return f"test_{req_counter}_{int(time.time())}"


def decode_msg(raw):
    """Decode WebSocket message (may be binary or text)."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


async def test_ws_bridge():
    # 1. Generate signed WebSocket URL
    print("[1] Generating signed WebSocket URL...")
    print(f"    Runtime: {RUNTIME_ARN}")
    print(f"    Session: {SESSION_ID}")

    from bedrock_agentcore.runtime import AgentCoreRuntimeClient

    client = AgentCoreRuntimeClient(region=REGION)
    presigned_url = client.generate_presigned_url(
        runtime_arn=RUNTIME_ARN,
        session_id=SESSION_ID,
        expires=300,
    )
    print(f"    URL: {presigned_url[:100]}...")

    # 2. Connect via WebSocket
    print(f"\n[2] Connecting WebSocket...")
    try:
        ws = await websockets.connect(presigned_url, open_timeout=30)
    except Exception as e:
        print(f"    FAILED: {e}")
        return False
    print(f"    Connected!")

    # 3. Fetch gateway token (for OpenClaw connect handshake)
    if not GATEWAY_TOKEN:
        print(f"\n[3] Fetching gateway token from Secrets Manager...")
        import boto3

        sm = boto3.client("secretsmanager", region_name=REGION)
        resp = sm.get_secret_value(SecretId="openclaw/gateway-token")
        token = resp["SecretString"]
        print(f"    Token fetched ({len(token)} chars)")
    else:
        token = GATEWAY_TOKEN
        print(f"\n[3] Using provided gateway token")

    # 4. Send Gateway Protocol connect request immediately
    # AgentCore platform auto-bridges to container port 18789 (OpenClaw Gateway).
    # No connect.challenge is sent — send connect request directly.
    connect_id = next_id()
    connect_req = {
        "type": "req",
        "id": connect_id,
        "method": "connect",
        "params": {
            "minProtocol": 3,
            "maxProtocol": 3,
            "client": {
                "id": "openclaw-control-ui",
                "version": "1.0.0",
                "platform": "linux",
                "mode": "backend",
            },
            "role": "operator",
            "scopes": ["operator.admin", "operator.read", "operator.write"],
            "caps": [],
            "commands": [],
            "permissions": {},
            "auth": {"token": token},
            "locale": "en-US",
            "userAgent": "openclaw-ws-poc/1.0",
        },
    }
    print(f"\n[4] Sending Gateway connect request...")
    await ws.send(json.dumps(connect_req))

    # 5. Wait for hello-ok response
    print(f"\n[5] Waiting for hello-ok response...")
    hello_ok = False
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=15)
        data = json.loads(decode_msg(msg))
        if data.get("type") == "res" and data.get("ok"):
            payload = data.get("payload", {})
            proto = payload.get("protocol", "?")
            methods_count = len(payload.get("features", {}).get("methods", []))
            events_count = len(payload.get("features", {}).get("events", []))
            print(f"    OK — hello-ok received!")
            print(f"    Protocol: v{proto}")
            print(f"    Methods: {methods_count}, Events: {events_count}")
            print(f"    Server: {json.dumps(payload.get('server', {}))}")
            hello_ok = True
        elif data.get("type") == "res" and not data.get("ok"):
            print(
                f"    AUTH FAILED: {json.dumps(data.get('error', {}), indent=2)}"
            )
            await ws.close()
            return False
        else:
            print(f"    UNEXPECTED: {decode_msg(msg)[:300]}")
    except asyncio.TimeoutError:
        print(f"    TIMEOUT — No response in 15s")
        await ws.close()
        return False

    if not hello_ok:
        await ws.close()
        return False

    # 6. Send chat.send
    print(f"\n[6] Sending chat.send 'Reply with exactly: WS_BRIDGE_OK'...")
    chat_id = next_id()
    await ws.send(
        json.dumps(
            {
                "type": "req",
                "id": chat_id,
                "method": "chat.send",
                "params": {
                    "sessionKey": "global",
                    "message": "Reply with exactly: WS_BRIDGE_OK",
                    "idempotencyKey": next_id(),
                },
            }
        )
    )

    # 7. Collect streaming responses
    print(f"\n[7] Collecting streaming responses (90s timeout)...")
    response_text = ""
    got_final = False
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            remaining = max(1, deadline - time.time())
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            data = json.loads(decode_msg(msg))
            msg_type = data.get("type", "?")
            event_name = data.get("event", "")

            if msg_type == "event" and event_name == "chat":
                payload = data.get("payload", {})
                state = payload.get("state", "")
                msg_obj = payload.get("message", {})
                content = msg_obj.get("content", [])
                text = ""
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                if state == "delta" and text:
                    new_chars = text[len(response_text):]
                    if new_chars:
                        sys.stdout.write(new_chars)
                        sys.stdout.flush()
                    response_text = text
                elif state == "final":
                    got_final = True
                    if text:
                        response_text = text
                    print(
                        f"\n    [final] response length: {len(response_text)} chars"
                    )
                    break
            elif msg_type == "res" and data.get("id") == chat_id:
                ok = data.get("ok")
                if not ok:
                    err = data.get("error", {})
                    print(f"\n    Chat REJECTED: {json.dumps(err)[:200]}")
                    break
            # silently skip other broadcasts (health, agent, presence, etc.)
    except asyncio.TimeoutError:
        if response_text:
            print(
                f"\n    (timeout but got partial response: {len(response_text)} chars)"
            )
        else:
            print(f"\n    TIMEOUT — no chat response received")

    # 8. Summary
    print(f"\n{'='*60}")
    passed = hello_ok and (got_final or bool(response_text))
    print(f"RESULT: {'PASS' if passed else 'FAIL'}")
    print(f"  WebSocket connected:  YES")
    print(f"  Gateway handshake:    {'YES' if hello_ok else 'NO'}")
    print(
        f"  Chat response:        {'YES' if response_text else 'NO'} ({len(response_text)} chars)"
    )
    if response_text:
        print(f"  Response preview:     {response_text[:200]}")
    print(f"{'='*60}")

    await ws.close()
    return passed


if __name__ == "__main__":
    result = asyncio.run(test_ws_bridge())
    sys.exit(0 if result else 1)
