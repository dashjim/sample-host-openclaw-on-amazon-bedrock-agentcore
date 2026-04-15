#!/bin/bash
# Deploy Wazuh Agent PoC to AgentCore Runtime (VPC mode)
# Prerequisites:
#   - Wazuh Manager running on an EC2 in the same VPC
#   - EC2 SG allows TCP 1514-1515 inbound from VPC CIDR
#   - Two private subnets with NAT gateway
#   - A security group for AgentCore Runtime (all outbound allowed)
#
# Usage:
#   export WAZUH_MANAGER_IP=172.31.x.x
#   export VPC_SUBNETS="subnet-aaa,subnet-bbb"
#   export VPC_SG="sg-xxx"
#   ./deploy-to-agentcore.sh
set -e

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
MANAGER_IP="${WAZUH_MANAGER_IP:?Set WAZUH_MANAGER_IP to your Wazuh Manager private IP}"
SUBNETS="${VPC_SUBNETS:?Set VPC_SUBNETS to comma-separated subnet IDs}"
SG="${VPC_SG:?Set VPC_SG to AgentCore security group ID}"
AGENT_NAME="${AGENT_NAME:-wazuh_runtime_poc}"

export AWS_DEFAULT_REGION=$REGION

IFS=',' read -ra SUBNET_ARRAY <<< "$SUBNETS"
SUBNET_JSON=$(printf '"%s",' "${SUBNET_ARRAY[@]}" | sed 's/,$//')

echo "=== AgentCore Wazuh PoC Deployment ==="
echo "  Region:     $REGION"
echo "  Manager IP: $MANAGER_IP"
echo "  Subnets:    $SUBNETS"
echo "  SG:         $SG"
echo ""

cd "$(dirname "$0")"

python3 << PYEOF
import json, time, os, shutil

os.environ["AWS_DEFAULT_REGION"] = "$REGION"

# Ensure Dockerfile is in place
shutil.copy("Dockerfile.runtime", "Dockerfile")

from bedrock_agentcore_starter_toolkit import Runtime

rt = Runtime()

print("=== Step 1: Configure (VPC mode) ===")
rt.configure(
    entrypoint="wazuh_runtime_agent.py",
    auto_create_execution_role=True,
    auto_create_ecr=True,
    requirements_file="requirements.txt",
    region="$REGION",
    agent_name="$AGENT_NAME",
    deployment_type="container",
    vpc_enabled=True,
    vpc_subnets=[$SUBNET_JSON],
    vpc_security_groups=["$SG"],
)

print("\n=== Step 2: Launch (local Docker build) ===")
launch_result = rt.launch(
    local_build=True,
    env_vars={
        "WAZUH_MANAGER_IP": "$MANAGER_IP",
        "WAZUH_AGENT_NAME": "agentcore-runtime-poc",
    },
)
print(f"Agent ID:  {launch_result.agent_id}")
print(f"Agent ARN: {launch_result.agent_arn}")
print(f"ECR URI:   {launch_result.ecr_uri}")

# Save for later use
with open("/tmp/wazuh_agentcore_deploy.json", "w") as f:
    json.dump({
        "agent_id": launch_result.agent_id,
        "agent_arn": launch_result.agent_arn,
        "ecr_uri": launch_result.ecr_uri,
    }, f, indent=2)

print("\n=== Step 3: Wait for READY ===")
sr = rt.status()
s = sr.endpoint["status"]
print(f"Status: {s}")
while s not in ["READY", "CREATE_FAILED", "DELETE_FAILED", "UPDATE_FAILED"]:
    time.sleep(15)
    sr = rt.status()
    s = sr.endpoint["status"]
    print(f"Status: {s}")

if s != "READY":
    print(f"FAILED: {s}")
    exit(1)

print("\nRuntime is READY.")
print("Saved deployment info to /tmp/wazuh_agentcore_deploy.json")
print()
print("Next steps:")
print("  1. Invoke to initialize:  aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn <ARN> --qualifier DEFAULT --payload '{\"prompt\":\"run\"}' --outfile /tmp/result.json")
print("  2. Or use rexec.sh to run shell commands inside the container")
print("  3. Use InvokeAgentRuntimeCommand to register and start Wazuh (see README)")
PYEOF

echo ""
echo "=== Deployment complete ==="
