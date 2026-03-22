# Skill Security Evaluation - Design Document

**Date**: 2026-03-22
**Branch**: `feature/skill-eval-runtime`
**Status**: Implemented

## Overview

Per-user skill security scanning using [sample-agent-skill-eval](https://github.com/aws-samples/sample-agent-skill-eval). Scans user-installed skills (ClawHub skills + custom skills in `.openclaw/skills/`) for security issues: secret leaks, injection surfaces, unsafe shell commands, supply chain risks, and more.

Two scan modes:
- **Audit** (static analysis, seconds) - No AI model needed. Checks secrets, dangerous patterns, structure compliance
- **Eval** (AI-powered, minutes) - Uses Claude CLI on Bedrock for functional correctness and trigger precision testing

## Architecture

```
Admin UI (Files page)
  |-- "Scan" button
  v
API Gateway --> Admin Lambda
  |-- POST /api/skill-eval/{namespace}
  v
Admin Lambda --> lambda:Invoke
  v
Skill Eval Lambda (Container: Python 3.12 + Node.js 22 + Claude CLI + skill-eval)
  |-- 1. Download .openclaw/skills/ from S3
  |-- 2. Run skill-eval audit/report per skill
  |-- 3. Save results to DynamoDB (SKILLSCAN#latest)
  |-- 4. Upload HTML reports to S3 ({namespace}/_skill-eval/)
  v
EventBridge (daily) --> Skill Eval Lambda (action: scan-all)
  |-- Enumerate all user namespaces
  |-- Audit each user's skills
```

## Container Image

```dockerfile
FROM public.ecr.aws/lambda/python:3.12-arm64

# Node.js 22 (for Claude CLI)
RUN dnf install -y tar gzip xz && \
    curl -fsSL https://nodejs.org/dist/v22.16.0/node-v22.16.0-linux-arm64.tar.xz | \
    tar -xJ --strip-components=1 -C /usr/local

# Claude CLI (configured to use Bedrock)
RUN npm install -g @anthropic-ai/claude-code@latest

# skill-eval (from local source)
COPY skill_eval_src/ /tmp/skill_eval_src/
RUN pip install /tmp/skill_eval_src/ pyyaml boto3
```

Environment variables:
```
CLAUDE_CODE_USE_BEDROCK=1
ANTHROPIC_DEFAULT_SONNET_MODEL=us.anthropic.claude-sonnet-4-6
ANTHROPIC_DEFAULT_HAIKU_MODEL=us.anthropic.claude-haiku-4-5-20251001-v1:0
ANTHROPIC_DEFAULT_OPUS_MODEL=us.anthropic.claude-opus-4-6-v1
```

## Lambda Actions

| Action | Trigger | Duration | Description |
|--------|---------|----------|-------------|
| `audit` | Admin UI "Scan" button | ~5-30s | Static security scan per user |
| `eval` | Admin UI "Deep Eval" button | ~2-5min | AI-powered functional + trigger eval |
| `scan-all` | EventBridge daily | ~minutes | Enumerate all namespaces, audit each |

## DynamoDB Schema

| PK | SK | Purpose |
|----|----|---------|
| `USER#{userId}` | `SKILLSCAN#latest` | Most recent scan result for a user |
| `USER#{userId}` | `SKILLSCAN#{timestamp}` | Historical scan record |

Scan record structure:
```json
{
  "score": 85,
  "grade": "B",
  "totalSkills": 3,
  "totalCriticals": 0,
  "skills": [
    {
      "name": "jina-reader",
      "score": 96,
      "grade": "A",
      "criticals": 0,
      "warnings": 0,
      "findings": [...]
    }
  ],
  "scannedAt": "2026-03-22T12:00:00Z",
  "scanType": "audit"
}
```

## Admin API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/skill-eval/{namespace}` | Get latest scan result |
| `POST` | `/api/skill-eval/{namespace}` | Trigger scan: `{"action": "audit"}` or `{"action": "eval"}` |

## Admin UI

### Files Page - Skill Security Panel
- Appears above file browser when a user is selected
- Shows: overall grade badge, score, last scan time
- "Scan" button triggers audit
- Expandable per-skill details with findings table (severity, code, message, file)

### Dashboard - Overview (future)
- Aggregate grade distribution across all users
- Critical findings summary

## CDK Resources (in OpenClawAdmin stack)

- `openclaw-skill-eval` - DockerImageFunction (ARM64, 1024MB, 15min timeout)
- EventBridge Rule `openclaw-skill-eval-daily` - `rate(1 day)` schedule
- CloudWatch Log Group `/openclaw/lambda/skill-eval`
- IAM: DynamoDB read/write, S3 read/write, Bedrock invoke, KMS

## API Gateway Route Migration

The original per-path route registration (12 paths x 4 methods = 48 Lambda permissions) hit the Lambda resource-based policy 20KB limit. Migrated to a single catch-all `/{proxy+}` route (4 permissions). This required a two-phase deployment:
1. Deploy with zero routes (clears all 48 permissions)
2. Deploy with catch-all route (adds 4 permissions)

The Lambda handler's internal router (`_match_route()`) still handles path-based dispatching.

## cdk.json Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `skill_eval_lambda_timeout_seconds` | `900` | Max 15 minutes for full eval |
| `skill_eval_lambda_memory_mb` | `1024` | Memory for container Lambda |
| `skill_eval_schedule` | `rate(1 day)` | Daily scan frequency |
| `skill_eval_enabled` | `true` | Enable/disable scheduled scanning |

## Security

- Skill eval Lambda has read-only S3 access to user skills + write for reports
- Bedrock access scoped to model invocation (InvokeModel + InvokeModelWithResponseStream)
- Claude CLI uses Bedrock (not Anthropic API) - no external API key needed
- Results stored under user's DynamoDB PK - consistent with existing identity model
- HTML reports stored in user's S3 namespace - accessible via Admin UI presigned URL
