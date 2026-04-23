#!/usr/bin/env python3
"""
WebSocket bridge POC test — verifies browser can reach OpenClaw Gateway Protocol
via AgentCore platform auto-bridge.

Usage:
    source ~/ws-poc-env.sh
    python3 test_ws_bridge.py                        # default test message
    python3 test_ws_bridge.py "你能干什么？"          # custom message
    python3 test_ws_bridge.py --interactive           # interactive chat mode

Requires: bedrock-agentcore SDK, websockets, boto3
"""

import asyncio
import json
import os
import sys
import time

import boto3
import websockets

RUNTIME_ARN = os.environ.get(
    "RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-west-2:576186206185:runtime/openclaw_agent-FMElB5ECU7",
)
SESSION_ID = os.environ.get(
    "SESSION_ID", "ses_user_c1874612116a454b_69185b4354bc"
)
ACTOR_ID = os.environ.get("ACTOR_ID", "feishu:ou_75f0be3af5be36553d6ab69fc561a12a")
USER_ID = os.environ.get("USER_ID", "user_c1874612116a454b")
CHANNEL = os.environ.get("CHANNEL", "feishu")
REGION = os.environ.get("AWS_REGION", "us-west-2")
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")

req_counter = 0


def next_id():
    global req_counter
    req_counter += 1
    return f"test_{req_counter}_{int(time.time())}"


def decode_msg(raw):
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def read_response(resp):
    """Read boto3 response body — handles both StreamingBody and str."""
    raw = resp.get("response", "{}")
    if hasattr(raw, "read"):
        return raw.read().decode()
    return str(raw)


def invoke_runtime(agentcore, action_payload):
    """invoke_agent_runtime — boto3 SDK takes raw JSON, NOT base64."""
    resp = agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=SESSION_ID,
        runtimeUserId=ACTOR_ID,
        payload=json.dumps(action_payload),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(read_response(resp))


def warmup_session():
    """HTTP bootstrap — ensures container is alive before WebSocket connect."""
    print("[0] HTTP bootstrap — warming up container...")

    agentcore = boto3.client("bedrock-agentcore", region_name=REGION)

    # Send warmup — auto-creates new container if session expired
    result = invoke_runtime(agentcore, {
        "action": "warmup",
        "userId": USER_ID,
        "actorId": ACTOR_ID,
        "channel": CHANNEL,
    })
    print(f"    Warmup raw: {json.dumps(result)[:200]}")
    status = result.get("status", "")
    if status == "ready":
        print("    Container already ready!")
        return True

    # Poll for openclawReady via status action
    # Container may return "internal error" during init — that's normal, keep polling
    print("    Waiting for OpenClaw...", end="", flush=True)
    t0 = time.time()
    for i in range(30):  # 30 * 2s = 60s max
        time.sleep(2)
        try:
            result = invoke_runtime(agentcore, {"action": "status"})
            # status action returns {"response": "{\"openclawReady\":true,...}"}
            inner = result.get("response", "")
            if isinstance(inner, str) and inner.startswith("{"):
                data = json.loads(inner)
                if data.get("openclawReady"):
                    elapsed = int(time.time() - t0)
                    print(f" ready! ({elapsed}s)")
                    return True
                print(".", end="", flush=True)
            elif result.get("status") == "ready":
                elapsed = int(time.time() - t0)
                print(f" ready! ({elapsed}s)")
                return True
            else:
                print(".", end="", flush=True)
        except Exception:
            print(".", end="", flush=True)

    print(" timeout!")
    return False


def get_gateway_token():
    """Fetch OpenClaw gateway token from Secrets Manager."""
    global GATEWAY_TOKEN
    if GATEWAY_TOKEN:
        return GATEWAY_TOKEN
    sm = boto3.client("secretsmanager", region_name=REGION)
    GATEWAY_TOKEN = sm.get_secret_value(SecretId="openclaw/gateway-token")["SecretString"]
    return GATEWAY_TOKEN


async def gateway_connect(ws):
    """Send Gateway Protocol connect and wait for hello-ok."""
    token = get_gateway_token()
    connect_id = next_id()
    await ws.send(json.dumps({
        "type": "req",
        "id": connect_id,
        "method": "connect",
        "params": {
            "minProtocol": 3, "maxProtocol": 3,
            "client": {"id": "openclaw-control-ui", "version": "1.0.0",
                       "platform": "linux", "mode": "backend"},
            "role": "operator",
            "scopes": ["operator.admin", "operator.read", "operator.write"],
            "caps": [], "commands": [], "permissions": {},
            "auth": {"token": token},
            "locale": "en-US", "userAgent": "openclaw-ws-poc/1.0",
        },
    }))

    msg = await asyncio.wait_for(ws.recv(), timeout=15)
    data = json.loads(decode_msg(msg))
    if data.get("type") == "res" and data.get("ok"):
        payload = data["payload"]
        print(f"    Gateway: v{payload.get('protocol')} "
              f"({len(payload.get('features',{}).get('methods',[]))} methods, "
              f"{len(payload.get('features',{}).get('events',[]))} events) "
              f"server={payload.get('server',{}).get('version','?')}")
        return True
    elif data.get("type") == "res":
        print(f"    Gateway connect FAILED: {json.dumps(data.get('error',{}))[:200]}")
    return False


async def chat_send(ws, message):
    """Send a chat message and stream the response."""
    chat_id = next_id()
    await ws.send(json.dumps({
        "type": "req", "id": chat_id, "method": "chat.send",
        "params": {
            "sessionKey": "global",
            "message": message,
            "idempotencyKey": next_id(),
        },
    }))

    response_text = ""
    try:
        deadline = time.time() + 120
        while time.time() < deadline:
            msg = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time()))
            data = json.loads(decode_msg(msg))

            if data.get("type") == "event" and data.get("event") == "chat":
                payload = data.get("payload", {})
                state = payload.get("state", "")
                content = payload.get("message", {}).get("content", [])
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
                    if text:
                        new_chars = text[len(response_text):]
                        if new_chars:
                            sys.stdout.write(new_chars)
                        response_text = text
                    print()
                    return response_text
            elif data.get("type") == "res" and data.get("id") == chat_id:
                if not data.get("ok"):
                    print(f"\n[error] {json.dumps(data.get('error',{}))[:200]}")
                    return ""
    except asyncio.TimeoutError:
        print(f"\n[timeout after {int(time.time()-deadline+120)}s]")

    return response_text


SYSTEM_COMMANDS = {
    "/health":      ("health", {}),
    "/status":      ("status", {}),
    "/sessions":    ("sessions.list", {}),
    "/history":     ("chat.history", {"sessionKey": "global"}),
    "/files":       ("agents.files.list", {"agentId": "main"}),
    "/skills":      ("skills.status", {"agentId": "main"}),
    "/tools":       ("tools.catalog", {"agentId": "main"}),
    "/models":      ("models.list", {}),
    "/usage":       ("usage.status", {}),
    "/cron":        ("cron.list", {}),
    "/config":      ("config.get", {}),
    "/diagnostics": ("diagnostics.stability", {}),
}


async def send_rpc(ws, method, params=None):
    """Send a Gateway RPC request and collect the response, skipping broadcast events."""
    req_id = next_id()
    await ws.send(json.dumps({
        "type": "req", "id": req_id, "method": method,
        "params": params or {},
    }))

    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            msg = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time()))
            data = json.loads(decode_msg(msg))
            if data.get("type") == "res" and data.get("id") == req_id:
                return data
            # Skip broadcast events (health, agent, presence, etc.)
    except asyncio.TimeoutError:
        return {"ok": False, "error": {"code": "TIMEOUT", "message": f"No response for {method} in 15s"}}
    except websockets.exceptions.ConnectionClosed as e:
        return {"ok": False, "error": {"code": "CONNECTION_CLOSED", "message": str(e)}}


def format_rpc_response(data):
    """Pretty-print an RPC response."""
    if not data:
        return "  (no response)"
    ok = data.get("ok")
    if ok:
        payload = data.get("payload", {})
        return json.dumps(payload, indent=2, ensure_ascii=False)
    else:
        err = data.get("error", {})
        return f"  ERROR: {json.dumps(err, ensure_ascii=False)}"


async def handle_system_command(ws, cmd_str, dump_mode=False):
    """Handle /command inputs — maps to Gateway Protocol RPC methods."""
    parts = cmd_str.strip().split(None, 2)
    cmd = parts[0].lower()

    if cmd == "/dump":
        return

    if cmd == "/raw":
        if len(parts) < 2:
            print("  Usage: /raw <method> [json_params]")
            return
        method = parts[1]
        params = json.loads(parts[2]) if len(parts) > 2 else {}
        print(f"  → {method} {json.dumps(params) if params else ''}")
        resp = await send_rpc(ws, method, params)
        print(format_rpc_response(resp))
        return

    if cmd == "/files.get":
        if len(parts) < 2:
            print("  Usage: /files.get <path>")
            return
        path = parts[1]
        resp = await send_rpc(ws, "agents.files.get", {"agentId": "main", "path": path})
        if resp and resp.get("ok"):
            content = resp.get("payload", {}).get("content", "")
            print(f"  --- {path} ({len(content)} chars) ---")
            print(content[:2000])
            if len(content) > 2000:
                print(f"  ... ({len(content) - 2000} chars truncated)")
        else:
            print(format_rpc_response(resp))
        return

    if cmd == "/history":
        resp = await send_rpc(ws, "chat.history", {"sessionKey": "global"})
        if resp and resp.get("ok"):
            messages = resp.get("payload", {}).get("messages", resp.get("payload", {}).get("rows", []))
            if isinstance(messages, list):
                print(f"  --- {len(messages)} messages ---")
                for m in messages[-10:]:
                    role = m.get("role", "?")
                    content = m.get("content", "")
                    if isinstance(content, list):
                        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
                    else:
                        text = str(content)
                    preview = text[:120].replace("\n", " ")
                    print(f"  [{role}] {preview}")
            else:
                print(format_rpc_response(resp))
        else:
            print(format_rpc_response(resp))
        return

    if cmd in SYSTEM_COMMANDS:
        method, params = SYSTEM_COMMANDS[cmd]
        print(f"  → {method}")
        resp = await send_rpc(ws, method, params)
        print(format_rpc_response(resp))
        return

    print(f"  Unknown command: {cmd}")
    print(f"  Type /help or see the command list above")


async def test_ws_bridge(message="Reply with exactly: WS_BRIDGE_OK", interactive=False):
    # 0. HTTP bootstrap
    if not warmup_session():
        print("    FAILED: Container did not become ready")
        return False

    # 1. Generate presigned URL
    from bedrock_agentcore.runtime import AgentCoreRuntimeClient
    client = AgentCoreRuntimeClient(region=REGION)
    presigned_url = client.generate_presigned_url(
        runtime_arn=RUNTIME_ARN, session_id=SESSION_ID, expires=300)

    # 2. Connect WebSocket
    print(f"\n[1] Connecting WebSocket...")
    try:
        ws = await websockets.connect(presigned_url, open_timeout=30)
    except Exception as e:
        print(f"    FAILED: {e}")
        return False
    print(f"    Connected!")

    # 3. Gateway handshake
    print(f"[2] Gateway Protocol handshake...")
    if not await gateway_connect(ws):
        await ws.close()
        return False

    if interactive:
        print(f"\n--- Interactive mode ---")
        print(f"  Chat:    type message and press Enter")
        print(f"  System:  /command to call Gateway Protocol methods")
        print(f"  Quit:    /quit or Ctrl+C")
        print(f"")
        print(f"  Available /commands:")
        print(f"    /health              — Gateway health snapshot")
        print(f"    /status              — Gateway status summary")
        print(f"    /sessions            — List all sessions")
        print(f"    /history             — Chat history (current session)")
        print(f"    /files               — List workspace files")
        print(f"    /files.get <path>    — Read a workspace file")
        print(f"    /skills              — Installed skills status")
        print(f"    /tools               — Tool catalog")
        print(f"    /models              — Available models")
        print(f"    /usage               — Usage status")
        print(f"    /cron                — List cron schedules")
        print(f"    /config              — Current config snapshot")
        print(f"    /diagnostics         — Stability diagnostics")
        print(f"    /raw <method> [json] — Send arbitrary RPC method")
        print(f"    /dump                — Show raw frames for next response")
        print()

        dump_mode = False
        while True:
            try:
                user_input = input("You: ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            stripped = user_input.strip()
            if not stripped:
                continue
            if stripped.lower() in ("/quit", "/exit", "/q", "quit", "exit", "q"):
                break

            if stripped.startswith("/"):
                await handle_system_command(ws, stripped, dump_mode)
                if stripped == "/dump":
                    dump_mode = not dump_mode
                    print(f"  [dump mode {'ON' if dump_mode else 'OFF'}]")
                continue

            print("AI: ", end="", flush=True)
            await chat_send(ws, stripped)
        await ws.close()
        return True

    # Single message test
    print(f"[3] Sending: {message}")
    print("    ", end="", flush=True)
    response = await chat_send(ws, message)

    # Summary
    passed = bool(response)
    print(f"\n{'='*60}")
    print(f"RESULT: {'PASS' if passed else 'FAIL'}")
    print(f"  WebSocket:  Connected")
    print(f"  Gateway:    Authenticated (Protocol v3)")
    print(f"  Response:   {len(response)} chars")
    if response:
        print(f"  Preview:    {response[:200]}")
    print(f"{'='*60}")

    await ws.close()
    return passed


if __name__ == "__main__":
    interactive = "--interactive" in sys.argv or "-i" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    message = args[0] if args else "Reply with exactly: WS_BRIDGE_OK"

    result = asyncio.run(test_ws_bridge(message=message, interactive=interactive))
    sys.exit(0 if result else 1)
