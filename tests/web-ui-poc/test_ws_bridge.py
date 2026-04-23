#!/usr/bin/env python3
"""
WebSocket bridge POC test — verifies the /ws handler bridges to OpenClaw Gateway.

Usage:
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


async def test_ws_bridge():
    # 1. Generate signed WebSocket URL using bedrock-agentcore SDK
    print(f"[1] Generating signed WebSocket URL...")
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

    # 3. Wait for connect.challenge from OpenClaw Gateway
    # The bridge creates upstream WS on connect — Gateway may take a moment to send challenge
    print(f"\n[3] Waiting for connect.challenge (up to 20s)...")
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=20)
        data = json.loads(msg)
        print(f"    Received: {json.dumps(data, indent=2)[:200]}")
        if data.get("type") == "event" and data.get("event") == "connect.challenge":
            print(f"    OK — Got connect.challenge with nonce")
        else:
            print(f"    UNEXPECTED — Expected connect.challenge, got: {data.get('type')}/{data.get('event')}")
    except asyncio.TimeoutError:
        print(f"    TIMEOUT — No message received in 10s")
        await ws.close()
        return False

    # 4. Send Gateway Protocol connect request
    if not GATEWAY_TOKEN:
        # Fetch from Secrets Manager
        print(f"\n[4] Fetching gateway token from Secrets Manager...")
        import boto3
        sm = boto3.client("secretsmanager", region_name=REGION)
        resp = sm.get_secret_value(SecretId="openclaw/gateway-token")
        token = resp["SecretString"]
        print(f"    Token fetched ({len(token)} chars)")
    else:
        token = GATEWAY_TOKEN
        print(f"\n[4] Using provided gateway token")

    connect_id = next_id()
    connect_req = {
        "type": "req",
        "id": connect_id,
        "method": "connect",
        "params": {
            "minProtocol": 3,
            "maxProtocol": 3,
            "client": {
                "id": "ws-poc-test",
                "version": "0.1.0",
                "platform": "linux",
                "mode": "operator",
            },
            "role": "operator",
            "scopes": ["operator.read", "operator.write", "operator.admin"],
            "caps": [],
            "commands": [],
            "permissions": {},
            "auth": {"token": token},
            "locale": "en-US",
            "userAgent": "openclaw-ws-poc/0.1.0",
        },
    }
    print(f"    Sending connect request (id={connect_id})...")
    await ws.send(json.dumps(connect_req))

    # 5. Wait for hello-ok response
    print(f"\n[5] Waiting for hello-ok response...")
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=15)
        data = json.loads(msg)
        if data.get("type") == "res" and data.get("ok"):
            payload = data.get("payload", {})
            proto = payload.get("protocol", "?")
            methods_count = len(payload.get("features", {}).get("methods", []))
            events_count = len(payload.get("features", {}).get("events", []))
            print(f"    OK — hello-ok received!")
            print(f"    Protocol: v{proto}")
            print(f"    Methods: {methods_count}, Events: {events_count}")
            print(f"    Server: {json.dumps(payload.get('server', {}))}")
        elif data.get("type") == "res" and not data.get("ok"):
            print(f"    AUTH FAILED: {json.dumps(data.get('error', {}), indent=2)}")
            await ws.close()
            return False
        else:
            print(f"    UNEXPECTED: {json.dumps(data)[:300]}")
    except asyncio.TimeoutError:
        print(f"    TIMEOUT — No response in 15s")
        await ws.close()
        return False

    # 6. Send health check
    print(f"\n[6] Sending health request...")
    health_id = next_id()
    await ws.send(json.dumps({
        "type": "req", "id": health_id, "method": "health", "params": {}
    }))

    try:
        # May receive broadcast events before the health response
        for _ in range(10):
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            data = json.loads(msg)
            if data.get("type") == "res" and data.get("id") == health_id:
                print(f"    Health response: ok={data.get('ok')}")
                break
            else:
                etype = data.get("event", data.get("method", "?"))
                print(f"    (broadcast: {data.get('type')}/{etype})")
    except asyncio.TimeoutError:
        print(f"    TIMEOUT waiting for health response")

    # 7. Send chat.send
    print(f"\n[7] Sending chat.send 'Hello from WebSocket POC!'...")
    chat_id = next_id()
    await ws.send(json.dumps({
        "type": "req",
        "id": chat_id,
        "method": "chat.send",
        "params": {
            "sessionKey": "global",
            "message": "Reply with exactly: WS_BRIDGE_OK",
            "idempotencyKey": next_id(),
        },
    }))

    # 8. Collect streaming responses
    print(f"\n[8] Collecting streaming responses (60s timeout)...")
    response_text = ""
    got_final = False
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            msg = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time()))
            data = json.loads(msg)

            if data.get("type") == "event" and data.get("event") in ("chat", "session.message"):
                payload = data.get("payload", {})
                state = payload.get("state", "")
                if state == "delta":
                    text = payload.get("text", payload.get("delta", ""))
                    if text:
                        response_text += text
                        sys.stdout.write(text)
                        sys.stdout.flush()
                elif state == "final":
                    got_final = True
                    final_text = payload.get("text", "")
                    if final_text:
                        response_text = final_text
                    print(f"\n    [final] response length: {len(response_text)} chars")
                    break
            elif data.get("type") == "res" and data.get("id") == chat_id:
                print(f"\n    Chat accepted: ok={data.get('ok')}")
            else:
                etype = data.get("event", data.get("method", "?"))
                # Skip noisy broadcast events
                if data.get("type") != "event":
                    print(f"\n    (other: {data.get('type')}/{etype})")
    except asyncio.TimeoutError:
        if response_text:
            print(f"\n    (timeout but got partial response: {len(response_text)} chars)")
        else:
            print(f"\n    TIMEOUT — no chat response received")

    # 9. Summary
    print(f"\n{'='*60}")
    print(f"RESULT: {'PASS' if got_final or response_text else 'FAIL'}")
    print(f"  WebSocket connected: YES")
    print(f"  Gateway handshake: YES")
    print(f"  Chat response: {'YES' if response_text else 'NO'} ({len(response_text)} chars)")
    if response_text:
        print(f"  Response preview: {response_text[:200]}")
    print(f"{'='*60}")

    await ws.close()
    return bool(got_final or response_text)


if __name__ == "__main__":
    result = asyncio.run(test_ws_bridge())
    sys.exit(0 if result else 1)
