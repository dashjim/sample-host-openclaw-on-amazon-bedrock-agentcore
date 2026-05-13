# AgentCore Gateway Interceptor 实现 AIGC API Key 注入

**日期**: 2026-05-12  
**状态**: Technical Reference  
**目标读者**: 需要实现 "Agent 不持有 AIGC API Key" 安全架构的客户

---

## 1. 背景与需求

在 Agent 调用外部 AIGC 服务（如 LLM API、图像生成 API 等）时，传统做法是将 API Key 配置在 Agent 侧。这带来以下安全风险：

- Agent 运行环境泄露 key（日志、内存转储、prompt injection 攻击）
- 多 Agent 场景下 key 分散管理，难以统一轮换
- 无法按身份/租户做 key 隔离和审计

**目标**: Agent 仅持有身份凭证（JWT），由 Gateway 层统一注入 AIGC API Key。Agent 全程不接触 key。

---

## 2. 解决方案：Gateway Request Interceptor

Amazon Bedrock AgentCore Gateway 提供 **Request Interceptor** 机制，可以在请求到达下游服务之前，由一个 Lambda 函数对请求进行任意修改——包括注入认证头和修改请求体。

### 2.1 架构概览

```
┌─────────┐         ┌─────────────────────┐         ┌──────────────────┐
│  Agent  │         │  AgentCore Gateway  │         │  AIGC Service    │
│         │  JWT    │                     │ API Key │  (OpenAI etc.)   │
│ 只持有  │────────▶│ 1. JWT Authorizer   │────────▶│                  │
│ 身份JWT │         │ 2. Request Intercept│         │  接收注入的 Key  │
│         │         │ 3. Forward to Target│         │                  │
└─────────┘         └────────┬────────────┘         └──────────────────┘
                             │
                    ┌────────▼────────┐
                    │  Interceptor    │
                    │  Lambda         │
                    │                 │
                    │  从 Secrets Mgr │
                    │  获取 API Key   │
                    │  注入到请求中   │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ AWS Secrets     │
                    │ Manager         │
                    │                 │
                    │ 集中管理所有    │
                    │ AIGC API Keys   │
                    └─────────────────┘
```

### 2.2 核心能力

| 能力 | 说明 |
|---|---|
| **修改请求 Header** | 注入 `Authorization: Bearer <key>` 或自定义 header |
| **修改请求 Body** | 在 JSON-RPC body 中注入 `api_key` 字段或任意参数 |
| **身份识别** | 从用户 JWT 中提取租户/角色信息，映射不同 key |
| **请求拦截** | 可拒绝不合规请求（返回 `transformedGatewayResponse` 直接中断） |

---

## 3. Request Interceptor 输入/输出规范

### 3.1 输入 (Lambda Event)

```json
{
  "interceptorInputVersion": "1.0",
  "mcp": {
    "rawGatewayRequest": { ... },
    "gatewayRequest": {
      "path": "/mcp",
      "httpMethod": "POST",
      "headers": {
        "Authorization": "Bearer <user-jwt-token>",
        "Content-Type": "application/json"
      },
      "body": {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
          "name": "generate_image",
          "arguments": {
            "prompt": "a cat in space",
            "size": "1024x1024"
          }
        }
      }
    }
  }
}
```

### 3.2 输出 — 修改 Header（适合下游服务从 Header 读 key）

```json
{
  "interceptorOutputVersion": "1.0",
  "mcp": {
    "transformedGatewayRequest": {
      "headers": {
        "Authorization": "Bearer sk-xxxx-injected-key",
        "Content-Type": "application/json"
      },
      "body": {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
          "name": "generate_image",
          "arguments": {
            "prompt": "a cat in space",
            "size": "1024x1024"
          }
        }
      }
    }
  }
}
```

### 3.3 输出 — 修改 Body（适合下游服务从请求体读 key）

```json
{
  "interceptorOutputVersion": "1.0",
  "mcp": {
    "transformedGatewayRequest": {
      "headers": {
        "Content-Type": "application/json"
      },
      "body": {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
          "name": "generate_image",
          "arguments": {
            "prompt": "a cat in space",
            "size": "1024x1024",
            "api_key": "sk-xxxx-injected-key"
          }
        }
      }
    }
  }
}
```

**Header 和 Body 可以同时修改**，完全取决于下游 AIGC 服务期望从哪里读取凭证。

---

## 4. 实现示例

### 4.1 Interceptor Lambda 代码（Secrets Manager 方式）

```python
import json
import logging
import os
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Lambda 冷启动时初始化，后续复用
secrets_client = boto3.client("secretsmanager")
_cached_key = None
_cached_key_expiry = 0


def _get_aigc_key():
    """从 Secrets Manager 获取 AIGC API Key，带本地缓存。"""
    global _cached_key, _cached_key_expiry
    import time
    now = time.time()
    # 缓存 5 分钟，平衡安全性和性能
    if _cached_key and now < _cached_key_expiry:
        return _cached_key
    
    resp = secrets_client.get_secret_value(
        SecretId=os.environ["AIGC_KEY_SECRET_ARN"]
    )
    secret = json.loads(resp["SecretString"])
    _cached_key = secret["api_key"]
    _cached_key_expiry = now + 300  # 5 分钟缓存
    return _cached_key


def lambda_handler(event, context):
    logger.info("Interceptor invoked")
    
    mcp_data = event.get("mcp", {})
    gateway_request = mcp_data.get("gatewayRequest", {})
    headers = gateway_request.get("headers", {})
    body = gateway_request.get("body", {})
    
    # 可选：从用户 JWT 中识别租户，选择不同的 key
    # auth_header = headers.get("Authorization", "")
    # tenant_id = decode_jwt_get_tenant(auth_header)
    # key = get_key_for_tenant(tenant_id)
    
    aigc_key = _get_aigc_key()
    
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": {
                    "Authorization": f"Bearer {aigc_key}",
                    "Content-Type": "application/json",
                },
                "body": body,  # body 原样透传（或也可修改）
            }
        },
    }
```

### 4.2 Gateway 创建配置

```python
import boto3

client = boto3.client("bedrock-agentcore-control", region_name="us-west-2")

# 创建 Gateway 时注册 Interceptor
resp = client.create_gateway(
    name="aigc-credential-gateway",
    protocolType="MCP",
    roleArn=gateway_role_arn,
    authorizerType="CUSTOM_JWT",
    authorizerConfiguration={
        "customJWTAuthorizer": {
            "discoveryUrl": f"https://cognito-idp.us-west-2.amazonaws.com/{pool_id}/.well-known/openid-configuration",
            "allowedClients": [app_client_id],
        }
    },
    interceptorConfigurations=[
        {
            "interceptor": {"lambda": {"arn": interceptor_lambda_arn}},
            "interceptionPoints": ["REQUEST"],
            "inputConfiguration": {"passRequestHeaders": True},  # 必须设为 True
        }
    ],
)
```

### 4.3 Terraform 配置（IaC 方式）

```hcl
resource "aws_bedrockagentcore_gateway" "aigc_gateway" {
  name        = "aigc-credential-gateway"
  role_arn    = aws_iam_role.gateway.arn

  authorizer_type = "CUSTOM_JWT"
  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = "https://cognito-idp.${var.region}.amazonaws.com/${var.user_pool_id}/.well-known/openid-configuration"
      allowed_clients = [var.app_client_id]
    }
  }

  protocol_type = "MCP"

  # 注册 Request Interceptor
  interceptor_configuration {
    interception_points = ["REQUEST"]

    interceptor {
      lambda {
        arn = aws_lambda_function.credential_injector.arn
      }
    }

    input_configuration {
      pass_request_headers = true  # 关键：允许 Interceptor 访问 JWT
    }
  }
}
```

---

## 5. 多租户场景：按身份分配不同 Key

当不同客户/租户需要使用各自的 AIGC API Key 时：

```python
import base64, json

def _decode_jwt_tenant(headers):
    """从 JWT 中提取租户标识（无需验签，Gateway 已验证）。"""
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return "default"
    token = auth[7:]
    parts = token.split(".")
    if len(parts) != 3:
        return "default"
    pad = parts[1] + "=" * (4 - len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(pad).decode())
    return payload.get("custom:tenantId", "default")


def lambda_handler(event, context):
    mcp_data = event.get("mcp", {})
    gateway_request = mcp_data.get("gatewayRequest", {})
    headers = gateway_request.get("headers", {})
    body = gateway_request.get("body", {})
    
    # 按租户获取对应的 key
    tenant_id = _decode_jwt_tenant(headers)
    secret_arn = f"arn:aws:secretsmanager:us-west-2:123456789012:secret:aigc-keys/{tenant_id}"
    
    resp = secrets_client.get_secret_value(SecretId=secret_arn)
    key = json.loads(resp["SecretString"])["api_key"]
    
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "headers": {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                "body": body,
            }
        },
    }
```

---

## 6. 替代方案对比

| 方案 | 描述 | 适用场景 | 复杂度 |
|---|---|---|---|
| **Request Interceptor + Secrets Manager** | Lambda 从 Secrets Manager 取 key 注入 | API Key 鉴权的 AIGC 服务（大多数场景） | 中 |
| **AgentCore Credential Provider (OAuth2)** | Gateway 内置 OAuth2 客户端凭证流程 | AIGC 服务支持标准 OAuth2 | 低（零代码） |
| **AgentCore Credential Provider (API Key)** | Gateway 内置 API Key 注入到指定 Header | 简单 API Key，无需动态逻辑 | 最低 |
| **Request Interceptor + Token Exchange** | 用户 token 换取下游服务 token | 需要 M2M token exchange | 中 |

### 6.1 内置 API Key Credential Provider（最简方案）

如果不需要多租户、不需要动态逻辑，Gateway 内置的 API Key Provider 即可：

```python
# 创建 API Key Provider
client.create_api_key_credential_provider(
    name="aigc-api-key",
    apiKey="sk-xxxx-your-aigc-key"
)

# Gateway Target 绑定时使用
client.create_gateway_target(
    gatewayIdentifier=gateway_id,
    name="aigc-target",
    targetConfiguration={...},
    credentialProviderConfigurations=[{
        "credentialProviderType": "API_KEY",
        "credentialProvider": {
            "apiKey": {
                "providerArn": api_key_provider_arn,
                "credentialParameterName": "Authorization",
                "credentialLocation": "HEADER"
            }
        }
    }]
)
```

---

## 7. IAM 权限要求

### 7.1 Gateway Service Role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeLambdaInterceptor",
      "Effect": "Allow",
      "Action": ["lambda:InvokeFunction"],
      "Resource": "arn:aws:lambda:*:*:function:aigc-credential-injector*"
    }
  ]
}
```

### 7.2 Interceptor Lambda Execution Role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadSecrets",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:us-west-2:123456789012:secret:aigc-keys/*"
    },
    {
      "Sid": "BasicLambdaExecution",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

### 7.3 Interceptor Lambda Resource Policy

```python
# 允许 AgentCore Gateway 调用此 Lambda
lambda_client.add_permission(
    FunctionName="aigc-credential-injector",
    StatementId="AllowGatewayInvoke",
    Action="lambda:InvokeFunction",
    Principal="bedrock-agentcore.amazonaws.com",
    SourceArn=f"arn:aws:bedrock-agentcore:{region}:{account_id}:gateway/*"
)
```

---

## 8. 关键配置项

| 配置 | 值 | 说明 |
|---|---|---|
| `passRequestHeaders` | `true` | **必须**。否则 Interceptor 无法读取用户 JWT |
| `interceptionPoints` | `["REQUEST"]` | 在请求到达 Target 之前执行 |
| `interceptorOutputVersion` | `"1.0"` | 固定值，API 契约 |
| Lambda 超时 | 建议 10-30s | 含 Secrets Manager 调用 |
| Lambda 内存 | 建议 256MB+ | boto3 初始化需要内存 |
| Secret 缓存 TTL | 建议 5 分钟 | 平衡安全与性能 |

---

## 9. 安全优势

| 维度 | Agent 持有 Key（传统） | Gateway Interceptor 注入（推荐） |
|---|---|---|
| Key 暴露面 | Agent 运行环境、环境变量、日志 | 仅 Lambda 内存（短暂） |
| Key 轮换 | 需更新所有 Agent 配置 | 仅更新 Secrets Manager，Agent 无感 |
| 审计追踪 | 需在每个 Agent 中实现 | Gateway + CloudWatch 统一日志 |
| 多租户隔离 | Agent 需自行实现切换逻辑 | Interceptor 按 JWT claims 自动路由 |
| Prompt Injection 防护 | Agent 可能被诱导泄露 key | Agent 根本不知道 key 的存在 |

---

## 10. Reference

### 10.1 官方示例代码

| 参考 | 链接 |
|---|---|
| **Token Exchange Interceptor（完整实现）** | [14-token-exchange-at-request-interceptor/](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/02-AgentCore-gateway/14-token-exchange-at-request-interceptor) |
| Token Exchange Lambda 代码 | [terraform/lambda_src/gateway_interceptor/lambda_function.py](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/01-tutorials/02-AgentCore-gateway/14-token-exchange-at-request-interceptor/terraform/lambda_src/gateway_interceptor/lambda_function.py) |
| Token Exchange Terraform (Gateway + Interceptor) | [terraform/agentcore.tf](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/01-tutorials/02-AgentCore-gateway/14-token-exchange-at-request-interceptor/terraform/agentcore.tf) |
| **HR Agent Request Interceptor（Body 修改示例）** | [prerequisite/lambda/interceptors/request_interceptor.py](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/02-use-cases/role-based-hr-data-agent/prerequisite/lambda/interceptors/request_interceptor.py) |
| HR Agent Response Interceptor（DLP 脱敏） | [prerequisite/lambda/interceptors/response_interceptor.py](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/02-use-cases/role-based-hr-data-agent/prerequisite/lambda/interceptors/response_interceptor.py) |
| **Gateway 创建脚本（含 Interceptor 注册）** | [scripts/agentcore_gateway.py](https://github.com/awslabs/amazon-bedrock-agentcore-samples/blob/main/02-use-cases/role-based-hr-data-agent/scripts/agentcore_gateway.py) |
| AgentCore Gateway Tutorials 目录 | [01-tutorials/02-AgentCore-gateway/](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-tutorials/02-AgentCore-gateway) |

### 10.2 AWS 文档

| 文档 | 链接 |
|---|---|
| Amazon Bedrock AgentCore Gateway 概述 | https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore-gateway.html |
| Gateway Interceptors | https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore-gateway-interceptors.html |
| Gateway Credential Providers | https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore-gateway-credential-providers.html |
| AWS Secrets Manager | https://docs.aws.amazon.com/secretsmanager/latest/userguide/intro.html |

---

## 11. 总结

**回答：可以。** AgentCore Gateway 的 Request Interceptor 完全支持在请求转发前注入 API Key，Agent 无需持有任何 AIGC 服务凭证。

推荐实现路径：
1. **简单场景（单 key，无动态逻辑）**→ 使用 Gateway 内置 API Key Credential Provider
2. **标准场景（需缓存、审计）**→ Request Interceptor + Secrets Manager
3. **多租户场景（按身份路由 key）**→ Request Interceptor + JWT 解码 + Secrets Manager（按租户存储）

Interceptor 对请求的控制是完整的：**Header 和 Body 都可以任意修改**。
