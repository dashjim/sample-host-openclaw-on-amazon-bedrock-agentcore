"""
Wazuh Agent on AgentCore Runtime — Full PoC

1. Container starts as root
2. Registers + starts real Wazuh Agent (connects to external Manager)
3. Drops to non-root for AI agent entrypoint
4. On invoke: runs attack tests + triggers FIM events
"""

import os
import subprocess
import time
import json
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

MANAGER_IP = os.environ.get("WAZUH_MANAGER_IP", "172.31.25.13")
AGENT_NAME = os.environ.get("WAZUH_AGENT_NAME", "agentcore-runtime-poc")


def run_cmd(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout.strip() or r.stderr.strip() or "(empty)")
    except Exception as e:
        return -1, str(e)


def setup_wazuh():
    """Phase 1: Configure and start Wazuh Agent as root."""
    results = []
    results.append(f"[SETUP] Running as: {run_cmd('id')[1]}")
    results.append(f"[SETUP] Manager IP: {MANAGER_IP}")

    # Write ossec.conf
    conf = f"""<ossec_config>
  <client>
    <server>
      <address>{MANAGER_IP}</address>
      <port>1514</port>
      <protocol>tcp</protocol>
    </server>
    <enrollment>
      <enabled>yes</enabled>
      <manager_address>{MANAGER_IP}</manager_address>
      <agent_name>{AGENT_NAME}</agent_name>
    </enrollment>
  </client>
  <syscheck>
    <disabled>no</disabled>
    <frequency>60</frequency>
    <scan_on_start>yes</scan_on_start>
    <directories check_all="yes" realtime="yes" report_changes="yes">/app</directories>
    <directories check_all="yes">/tmp</directories>
  </syscheck>
  <rootcheck>
    <disabled>no</disabled>
  </rootcheck>
  <localfile>
    <log_format>syslog</log_format>
    <location>/app/app.log</location>
  </localfile>
</ossec_config>"""

    with open("/var/ossec/etc/ossec.conf", "w") as f:
        f.write(conf)
    run_cmd("chown root:wazuh /var/ossec/etc/ossec.conf && chmod 640 /var/ossec/etc/ossec.conf")

    # Network diagnostics — compact
    code, out = run_cmd("ip -4 addr show | grep inet | grep -v 127.0.0.1")
    results.append(f"[NET] IPs: {out}")
    code, out = run_cmd("ip route show")
    results.append(f"[NET] Routes: {out}")
    code, out = run_cmd(f"timeout 5 bash -c 'echo > /dev/tcp/{MANAGER_IP}/22' 2>&1 && echo 'port22=OK' || echo 'port22=FAIL'")
    results.append(f"[NET] SSH test: {out}")
    code, out = run_cmd(f"timeout 5 bash -c 'echo > /dev/tcp/{MANAGER_IP}/1515' 2>&1 && echo 'port1515=OK' || echo 'port1515=FAIL'")
    results.append(f"[NET] 1515 test: {out}")
    code, out = run_cmd(f"curl -v --connect-timeout 5 telnet://{MANAGER_IP}:1515 2>&1 | head -8")
    results.append(f"[NET] curl 1515: {out}")
    code, out = run_cmd(f"timeout 5 bash -c 'echo > /dev/tcp/{MANAGER_IP}/1514' 2>&1 && echo 'port1514=OK' || echo 'port1514=FAIL'")
    results.append(f"[NET] 1514 test: {out}")
    code, out = run_cmd(f"getent hosts {MANAGER_IP} 2>&1")
    results.append(f"[NET] DNS resolve: {out}")
    code, out = run_cmd("curl -s --connect-timeout 5 https://checkip.amazonaws.com 2>&1")
    results.append(f"[NET] NAT IP: {out}")

    # Register
    code, out = run_cmd(f"/var/ossec/bin/agent-auth -m {MANAGER_IP} -A {AGENT_NAME}", timeout=30)
    results.append(f"[SETUP] Registration: {out}")

    # Start
    code, out = run_cmd("/var/ossec/bin/wazuh-control start")
    results.append(f"[SETUP] Start: {out}")
    time.sleep(5)

    # Status
    code, out = run_cmd("/var/ossec/bin/wazuh-control status")
    results.append(f"[SETUP] Status:\n{out}")

    # Lock down for non-root
    run_cmd("chmod 750 /var/ossec")

    # Create non-root user and app dir
    run_cmd("useradd -m -s /bin/bash agentuser 2>/dev/null")
    run_cmd("mkdir -p /app && chown agentuser:agentuser /app")

    return results


# Lazy setup — don't block import (health check needs to pass first)
_setup_log = None


@app.entrypoint
async def invoke(payload=None):
    """If payload has 'cmd', run it directly and return. Otherwise full test."""
    if payload and payload.get("cmd"):
        code, out = run_cmd(payload["cmd"], timeout=30)
        return {"status": "success", "summary": f"[cmd] rc={code}\n{out}"}

    global _setup_log
    if _setup_log is None:
        _setup_log = setup_wazuh()
    results = list(_setup_log)
    results.append("")

    # Attack tests (as agentuser)
    results.append("=== Attack Tests (as agentuser) ===")
    attacks = [
        ("Kill Wazuh daemon",    "pkill -f wazuh-agentd"),
        ("Read Wazuh config",    "cat /var/ossec/etc/ossec.conf"),
        ("Modify Wazuh config",  "echo hacked >> /var/ossec/etc/ossec.conf"),
        ("Delete Wazuh binary",  "rm /var/ossec/bin/wazuh-agentd"),
        ("Stop Wazuh service",   "/var/ossec/bin/wazuh-control stop"),
        ("Read agent keys",      "cat /var/ossec/etc/client.keys"),
        ("List Wazuh dir",       "ls /var/ossec/"),
        ("Uninstall Wazuh",      "apt-get remove -y wazuh-agent"),
    ]

    blocked = 0
    for name, cmd in attacks:
        code, out = run_cmd(f"su - agentuser -c '{cmd}' 2>&1")
        is_blocked = (code != 0 or "denied" in out.lower() or
                      "not permitted" in out.lower() or "cannot" in out.lower())
        if is_blocked:
            blocked += 1
        tag = "BLOCKED" if is_blocked else "ALLOWED"
        results.append(f"  [{tag}] {name}: {out[:120]}")

    results.append(f"\n  Result: {blocked}/{len(attacks)} attacks blocked")

    # Trigger FIM events (as agentuser)
    results.append("\n=== FIM Trigger (as agentuser) ===")
    run_cmd("su - agentuser -c 'echo \"AI output\" > /app/output.txt' 2>&1")
    run_cmd("su - agentuser -c 'echo \"suspicious\" > /tmp/suspicious.sh' 2>&1")
    results.append("  Created /app/output.txt and /tmp/suspicious.sh")
    results.append("  (Check Wazuh Manager alerts after ~60s)")

    # Wazuh survival check
    results.append("\n=== Wazuh Survival ===")
    code, out = run_cmd("ps aux | grep wazuh | grep -v grep")
    alive = [l.split()[-1] for l in out.split("\n") if "/var/ossec" in l]
    results.append(f"  Processes alive: {len(alive)} — {', '.join(alive)}")

    code, out = run_cmd("/var/ossec/bin/wazuh-control status")
    results.append(f"  Status: {out}")

    summary = "\n".join(results)
    return {"status": "success", "summary": summary}


if __name__ == "__main__":
    app.run()
