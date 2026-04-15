#!/bin/bash
set -e

echo "============================================"
echo "  AgentCore Wazuh PoC - Privilege Separation"
echo "============================================"

# ---- Phase 1: Configure Wazuh Agent (as root) ----
echo "[ROOT] Configuring Wazuh Agent..."
MANAGER_IP="${WAZUH_MANAGER_IP:-wazuh-manager}"
AGENT_NAME="${WAZUH_AGENT_NAME:-agentcore-test}"

sed -e "s/WAZUH_MANAGER_IP/${MANAGER_IP}/g" \
    -e "s/WAZUH_AGENT_NAME/${AGENT_NAME}/g" \
    /var/ossec/etc/ossec.conf.template > /var/ossec/etc/ossec.conf

chown root:wazuh /var/ossec/etc/ossec.conf
chmod 640 /var/ossec/etc/ossec.conf

# ---- Phase 2: Register agent first, then start ----
echo "[ROOT] Registering agent with Manager at $MANAGER_IP..."
/var/ossec/bin/agent-auth -m "$MANAGER_IP" -A "$AGENT_NAME" 2>&1 || true

# Wait a moment for registration to complete
sleep 2

if [ -f /var/ossec/etc/client.keys ] && [ -s /var/ossec/etc/client.keys ]; then
    echo "[ROOT] Agent registered!"
    cat /var/ossec/etc/client.keys
else
    echo "[ROOT] WARNING: Registration may have failed, starting anyway..."
fi

echo "[ROOT] Starting Wazuh Agent..."
/var/ossec/bin/wazuh-control start || true

# Wait for agent processes to stabilize
sleep 5

# Show agent status
echo ""
echo "[ROOT] Wazuh Agent Status:"
/var/ossec/bin/wazuh-control status || true
echo ""
echo "[ROOT] Wazuh processes (owned by root/wazuh):"
ps aux | grep -E "wazuh|ossec" | grep -v grep
echo ""

# ---- Phase 3: Run AI App as non-root ----
echo "============================================"
echo "[ROOT] Dropping privileges to 'agentuser'..."
echo "============================================"

# Run the AI app as agentuser — this will also run attack tests
exec su - agentuser -c "python3 /app/ai_app.py"
