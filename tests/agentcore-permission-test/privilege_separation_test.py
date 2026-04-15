"""
Privilege Separation PoC for AgentCore Runtime

Simulates: root process (Wazuh) + non-root process (AI agent)
Tests whether non-root can tamper with root process/files.
"""

import os
import subprocess
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()


def run_cmd(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.returncode, r.stdout.strip() or r.stderr.strip() or "(empty)")
    except Exception as e:
        return (-1, str(e))


@app.entrypoint
async def invoke(payload=None):
    results = []

    # === Phase 1: Root setup (simulating Wazuh install) ===
    results.append("=== Phase 1: Root Setup (simulating Wazuh) ===")
    results.append(f"Current user: {run_cmd('id')[1]}")

    # Create "wazuh" directory and dummy daemon
    os.makedirs("/var/ossec/bin", exist_ok=True)
    os.makedirs("/var/ossec/etc", exist_ok=True)
    os.makedirs("/var/ossec/logs", exist_ok=True)

    # Write a dummy daemon script (simulates wazuh-agentd)
    with open("/var/ossec/bin/wazuh-daemon", "w") as f:
        f.write("#!/bin/bash\nwhile true; do echo \"[wazuh] monitoring...\" >> /var/ossec/logs/ossec.log; sleep 5; done\n")
    os.chmod("/var/ossec/bin/wazuh-daemon", 0o755)

    # Write config
    with open("/var/ossec/etc/ossec.conf", "w") as f:
        f.write("<ossec_config><client><server><address>wazuh-manager</address></server></client></ossec_config>\n")

    # Lock down ownership to root
    run_cmd("chown -R root:root /var/ossec")
    run_cmd("chmod 750 /var/ossec")

    # Start daemon as root (background)
    run_cmd("nohup /var/ossec/bin/wazuh-daemon &")
    daemon_pid = run_cmd("pgrep -f wazuh-daemon")[1]
    results.append(f"Wazuh daemon PID (root): {daemon_pid}")
    results.append(f"Wazuh daemon owner: {run_cmd(f'ps -o user= -p {daemon_pid}')[1] if daemon_pid.isdigit() else 'N/A'}")

    # Create non-root user
    run_cmd("useradd -m -s /bin/bash agentuser 2>/dev/null")
    results.append(f"Created agentuser: {run_cmd('id agentuser')[1]}")

    # === Phase 2: Non-root attacks (simulating malicious AI agent) ===
    results.append("")
    results.append("=== Phase 2: Non-root Attack Tests ===")

    attacks = [
        ("kill wazuh daemon",       f"kill {daemon_pid}" if daemon_pid.isdigit() else "echo no-pid"),
        ("read wazuh config",       "cat /var/ossec/etc/ossec.conf"),
        ("modify wazuh config",     "echo hacked >> /var/ossec/etc/ossec.conf"),
        ("delete wazuh binary",     "rm /var/ossec/bin/wazuh-daemon"),
        ("stop wazuh (control)",    "/var/ossec/bin/wazuh-daemon stop 2>&1 || echo 'cannot stop'"),
        ("list wazuh dir",          "ls -la /var/ossec/"),
        ("apt-get remove",          "apt-get remove -y --dry-run curl"),
        ("write to /etc",           "touch /etc/hacked"),
        ("install package",         "apt-get install -y nmap"),
        ("read /proc of daemon",    f"cat /proc/{daemon_pid}/status 2>&1 | head -3" if daemon_pid.isdigit() else "echo no-pid"),
    ]

    for name, cmd in attacks:
        # Run as agentuser (non-root)
        code, out = run_cmd(f"su - agentuser -c '{cmd}' 2>&1")
        blocked = code != 0 or "denied" in out.lower() or "operation not permitted" in out.lower() or "cannot" in out.lower()
        status = "BLOCKED" if blocked else "ALLOWED"
        results.append(f"  [{status}] {name}")
        results.append(f"    -> {out[:200]}")

    # === Phase 3: Verify daemon still running ===
    results.append("")
    results.append("=== Phase 3: Verify Wazuh Survived ===")
    still_running = run_cmd(f"ps -p {daemon_pid} -o pid= 2>/dev/null")[1].strip() if daemon_pid.isdigit() else ""
    results.append(f"Daemon still running: {'YES' if still_running else 'NO'}")
    log_content = run_cmd("cat /var/ossec/logs/ossec.log 2>/dev/null | tail -3")[1]
    results.append(f"Wazuh log intact: {log_content}")

    summary = "\n".join(results)
    return {"status": "success", "summary": summary}


if __name__ == "__main__":
    app.run()
