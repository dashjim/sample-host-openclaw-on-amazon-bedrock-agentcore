#!/bin/bash
# Execute shell command in AgentCore Runtime session via invoke_agent_runtime_command
# Usage: ./rexec.sh <session_id> "your command"
#   or:  ./rexec.sh "your command"  (auto-creates session via invoke first)
set -e
export AWS_DEFAULT_REGION=us-east-1

RUNTIME_ARN="arn:aws:bedrock-agentcore:us-east-1:576186206185:runtime/wazuh_runtime_poc-64aAlrCpYE"
TIMEOUT="${REXEC_TIMEOUT:-60}"

if [ $# -eq 2 ]; then
    SESSION_ID="$1"
    CMD="$2"
elif [ $# -eq 1 ]; then
    CMD="$1"
    # Need to get session ID — invoke first to create one
    SESSION_ID=$(python3 -c "
import boto3, json
client = boto3.client('bedrock-agentcore', region_name='us-east-1')
resp = client.invoke_agent_runtime(
    agentRuntimeArn='$RUNTIME_ARN', qualifier='DEFAULT',
    payload=json.dumps({'cmd': 'echo ready'})
)
print(resp.get('runtimeSessionId', ''))
")
    if [ -z "$SESSION_ID" ]; then
        echo "ERROR: Could not get session ID"
        exit 1
    fi
    echo "Session: $SESSION_ID"
fi

python3 << PYEOF
import boto3, json, sys

client = boto3.client('bedrock-agentcore', region_name='us-east-1')
try:
    resp = client.invoke_agent_runtime_command(
        agentRuntimeArn='$RUNTIME_ARN',
        runtimeSessionId='$SESSION_ID',
        qualifier='DEFAULT',
        contentType='application/json',
        accept='application/vnd.amazon.eventstream',
        body={'command': '/bin/bash -c "$CMD"', 'timeout': $TIMEOUT}
    )
    for event in resp.get('stream', []):
        if 'chunk' in event:
            chunk = event['chunk']
            if 'contentDelta' in chunk:
                d = chunk['contentDelta']
                if d.get('stdout'): print(d['stdout'], end='')
                if d.get('stderr'): print(d['stderr'], end='', file=sys.stderr)
            if 'contentStop' in chunk:
                s = chunk['contentStop']
                print(f"\n[exit={s.get('exitCode')} status={s.get('status')}]")
except Exception as e:
    print(f"Error: {e}")
PYEOF
