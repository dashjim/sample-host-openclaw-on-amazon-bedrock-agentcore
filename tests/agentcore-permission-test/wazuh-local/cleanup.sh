#!/bin/bash
# Clean up all Wazuh PoC test resources
# Reads deployment info from /tmp/wazuh_agentcore_deploy.json if available
set -e

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_DEFAULT_REGION=$REGION

# Try to load deployment info
if [ -f /tmp/wazuh_agentcore_deploy.json ]; then
    RUNTIME_ID=$(python3 -c "import json; print(json.load(open('/tmp/wazuh_agentcore_deploy.json'))['agent_id'])")
    ECR_URI=$(python3 -c "import json; print(json.load(open('/tmp/wazuh_agentcore_deploy.json'))['ecr_uri'])")
    ECR_REPO=$(echo "$ECR_URI" | cut -d'/' -f2 | cut -d':' -f1)
    echo "Loaded deployment info: Runtime=$RUNTIME_ID, ECR=$ECR_REPO"
else
    echo "No deployment info found. Set variables manually or skip."
    RUNTIME_ID="${RUNTIME_ID:-}"
    ECR_REPO="${ECR_REPO:-}"
fi

AGENTCORE_SG="${AGENTCORE_SG:-}"
EC2_SG="${EC2_SG:-}"

echo "=== 1. Delete AgentCore Runtime ==="
if [ -n "$RUNTIME_ID" ]; then
    aws bedrock-agentcore-control delete-agent-runtime --agent-runtime-id "$RUNTIME_ID" 2>&1 || echo "(skip)"
fi

echo "=== 2. Delete ECR repo ==="
if [ -n "$ECR_REPO" ]; then
    aws ecr delete-repository --repository-name "$ECR_REPO" --force 2>&1 || echo "(skip)"
fi

echo "=== 3. Clean EC2 SG Wazuh rules ==="
if [ -n "$EC2_SG" ]; then
    aws ec2 revoke-security-group-ingress --group-id "$EC2_SG" \
      --ip-permissions '[{"IpProtocol":"tcp","FromPort":1514,"ToPort":1515,"IpRanges":[{"CidrIp":"172.31.0.0/16"}]}]' 2>&1 || true
    echo "  EC2 SG cleaned"
fi

echo "=== 4. Delete AgentCore SG ==="
if [ -n "$AGENTCORE_SG" ]; then
    aws ec2 delete-security-group --group-id "$AGENTCORE_SG" 2>&1 || echo "(skip - ENI may still be attached, retry after 8h)"
fi

echo "=== 5. Clean IAM roles (auto-created by starter toolkit) ==="
for role in $(aws iam list-roles --query "Roles[?starts_with(RoleName,'AmazonBedrockAgentCoreSDK')].RoleName" --output text 2>/dev/null); do
    echo "  Cleaning $role"
    for arn in $(aws iam list-attached-role-policies --role-name "$role" --query 'AttachedPolicies[*].PolicyArn' --output text 2>/dev/null); do
        aws iam detach-role-policy --role-name "$role" --policy-arn "$arn" 2>/dev/null
    done
    for name in $(aws iam list-role-policies --role-name "$role" --query 'PolicyNames' --output text 2>/dev/null); do
        aws iam delete-role-policy --role-name "$role" --policy-name "$name" 2>/dev/null
    done
    aws iam delete-role --role-name "$role" 2>/dev/null || true
done

echo "=== 6. Uninstall Wazuh Manager (if installed on this host) ==="
if [ -f /var/ossec/bin/wazuh-control ]; then
    sudo /var/ossec/bin/wazuh-control stop 2>&1 || true
    sudo apt-get remove -y wazuh-manager 2>&1 | tail -3
    sudo rm -rf /var/ossec
    sudo rm -f /etc/apt/sources.list.d/wazuh.list /usr/share/keyrings/wazuh.gpg
fi

echo ""
echo "=== Cleanup done ==="
echo "Local Docker images: run 'docker image prune -f' to remove"
