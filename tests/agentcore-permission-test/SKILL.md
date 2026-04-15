# AgentCore Runtime 安全监控 PoC：Wazuh Agent 集成

在 Amazon Bedrock AgentCore Runtime 容器内运行 Wazuh Agent，实现对 AI agent 行为的实时安全监控。

## 方案原理

```
AgentCore Runtime (microVM)                    你的 VPC
┌──────────────────────────────┐              ┌──────────────────────┐
│  Container (root 启动)        │              │  Wazuh Manager (EC2) │
│                              │  TCP 1514    │                      │
│  Wazuh Agent (root)  ────────│─────────────>│  ├── 规则引擎         │
│  ├── FIM (inotify)           │  TCP 1515    │  ├── 告警日志         │
│  ├── 进程监控 (/proc)         │─────────────>│  └── Dashboard       │
│  ├── Rootkit 检测             │              │                      │
│  └── SCA 合规扫描             │              └──────────────────────┘
│                              │
│  AI Agent (non-root) ───     │
│  └── 业务代码 (agentuser)     │
│      无法篡改 Wazuh (/var/ossec 只有 root 可访问)
└──────────────────────────────┘
```

**核心机制**：容器不设 `USER` 指令 → 以 root 启动 Wazuh → `chmod 750 /var/ossec` 锁权限 → AI agent 以 non-root 运行。

### 关键实现文件

**1. Dockerfile 不设 USER（以 root 启动）** → [`wazuh-local/Dockerfile.runtime`](wazuh-local/Dockerfile.runtime)

```dockerfile
# 安装 Wazuh Agent
RUN WAZUH_MANAGER=placeholder apt-get install -y wazuh-agent

# 关键：不设 USER 指令 → 容器以 root 身份启动
# AgentCore Runtime 不强制降权，Dockerfile 是什么用户就是什么用户
EXPOSE 8080
CMD ["python", "-m", "wazuh_runtime_agent"]
```

**2. entrypoint.sh: root 启动 Wazuh → 降权运行 AI agent** → [`wazuh-local/entrypoint.sh`](wazuh-local/entrypoint.sh)

```bash
#!/bin/bash
# Phase 1: 以 root 注册并启动 Wazuh Agent
/var/ossec/bin/agent-auth -m "$MANAGER_IP" -A "$AGENT_NAME"
/var/ossec/bin/wazuh-control start

# Phase 2: 锁定 /var/ossec，非 root 无法访问
chmod 750 /var/ossec

# Phase 3: 降权为 non-root 运行 AI agent
exec su - agentuser -c "python3 /app/ai_app.py"
```

**3. chmod 750 /var/ossec 锁权限** → [`wazuh-local/Dockerfile.agent`](wazuh-local/Dockerfile.agent)

```dockerfile
# Wazuh 安装后自带 root:wazuh 属组，内部进程通过 wazuh group 通信
# 只需锁顶层目录，阻止 "other" 用户（agentuser）访问
RUN chmod 750 /var/ossec
```

效果：agentuser（不在 wazuh 组）无法进入 /var/ossec，无法读/改/删/停 Wazuh。

**4. AI agent 以 non-root 运行** → [`wazuh-local/ai_app.py`](wazuh-local/ai_app.py)

```python
# ai_app.py 以 agentuser (uid=1000) 身份运行
# 尝试 12 种攻击全部被阻断：
#   kill wazuh → Operation not permitted
#   cat /var/ossec/etc/ossec.conf → Permission denied
#   rm /var/ossec/bin/wazuh-agentd → Permission denied
#   apt-get remove wazuh-agent → Permission denied
#   ...
```

## 目录结构

```
agentcore-permission-test/
├── README.md                       # 本文件
├── requirements.txt                # Python 依赖 (bedrock-agentcore SDK)
├── Dockerfile                      # 权限检测 agent 的 Dockerfile
├── permission_test_agent.py        # 测试 1: AgentCore Runtime 权限环境检测
├── privilege_separation_test.py    # 测试 2: Root/Non-root 权限分离模拟
│
└── wazuh-local/                    # 测试 3: 真实 Wazuh Agent 集成
    ├── requirements.txt            # Python 依赖
    ├── Dockerfile.agent            # 本地 Docker 测试用 (Wazuh + AI App)
    ├── Dockerfile.runtime          # AgentCore Runtime 部署用
    ├── ossec.conf.template         # Wazuh Agent 配置模板
    ├── entrypoint.sh               # 本地测试: root 启动 Wazuh → su 降权
    ├── ai_app.py                   # 本地测试: 模拟 AI app + 攻击测试
    ├── wazuh_runtime_agent.py      # AgentCore Runtime 版: Wazuh + 攻击测试
    ├── rexec.sh                    # 工具: 通过 InvokeAgentRuntimeCommand 执行命令
    ├── deploy-to-agentcore.sh      # 一键部署到 AgentCore Runtime (VPC 模式)
    └── cleanup.sh                  # 清理所有测试资源
```

## 三轮测试

### 测试 1: AgentCore Runtime 权限环境检测

**目的**：确认 Runtime 容器的 UID、capabilities、/proc、inotify 等关键权限。

```bash
cd agentcore-permission-test

# 部署到 AgentCore Runtime
python3 -c "
from bedrock_agentcore_starter_toolkit import Runtime
rt = Runtime()
rt.configure(entrypoint='permission_test_agent.py', auto_create_execution_role=True,
    auto_create_ecr=True, requirements_file='requirements.txt',
    region='us-east-1', agent_name='perm_test', deployment_type='container')
result = rt.launch(local_build=True)
print(f'Agent ID: {result.agent_id}')
import time; time.sleep(15)
resp = rt.invoke({'prompt': 'run'})
print(resp)
"
```

**关键结论**:

| 项目 | Dockerfile 无 USER | Dockerfile 有 USER |
|------|-------------------|-------------------|
| UID | `0 (root)` | `1000 (bedrock_agentcore)` |
| CapEff | `0xa82425fb` | `0x0000000000000000` |
| 写 /etc | 可写 | 拒绝 |
| apt-get | 可用 | 拒绝 |
| /proc | 可访问 | 可访问 |
| inotify | 可用 (63287 watches) | 可用 |
| auditd | 不可用 | 不可用 |
| 架构 | aarch64 (ARM64) | aarch64 |

**结论**：AgentCore Runtime 不强制 non-root，Dockerfile 里是什么用户就是什么用户。

### 测试 2: 权限分离模拟

**目的**：验证 root Wazuh + non-root AI agent 的权限分离方案。

```bash
# 同上，使用 privilege_separation_test.py 作为 entrypoint
```

**结果**：12/12 攻击全部被阻断（kill、读/改/删 config、停止服务、卸载等）。

### 测试 3: 真实 Wazuh Agent 集成

#### 3a. 本地 Docker 测试（快速验证）

```bash
# 1. 安装 Wazuh Manager
curl -s https://packages.wazuh.com/key/GPG-KEY-WAZUH | sudo gpg --dearmor -o /usr/share/keyrings/wazuh.gpg
echo "deb [signed-by=/usr/share/keyrings/wazuh.gpg] https://packages.wazuh.com/4.x/apt/ stable main" | sudo tee /etc/apt/sources.list.d/wazuh.list
sudo apt-get update && sudo apt-get install -y wazuh-manager
sudo /var/ossec/bin/wazuh-control start

# 2. 运行 Agent 容器
cd wazuh-local
docker build -t wazuh-agent-test -f Dockerfile.agent .
docker run --rm \
  -e WAZUH_MANAGER_IP="172.17.0.1" \
  -e WAZUH_AGENT_NAME="local-test-agent" \
  --add-host=wazuh-manager:172.17.0.1 \
  wazuh-agent-test

# 3. 检查 Manager
sudo /var/ossec/bin/manage_agents -l
sudo tail -30 /var/ossec/logs/alerts/alerts.log
```

**结果**：Agent 注册成功，12/12 攻击阻断，FIM 告警发送到 Manager。

#### 3b. AgentCore Runtime 部署（完整验证）

**前提条件**：
- 同 VPC 内有一台 EC2 运行 Wazuh Manager
- EC2 安全组允许 TCP 1514-1515 入站（from VPC CIDR）
- 两个私有子网有 NAT Gateway
- 子网在 AgentCore 支持的 AZ（us-east-1: use1-az1, use1-az2, use1-az4）

```bash
cd wazuh-local

# 1. 创建 AgentCore 用的安全组
AGENTCORE_SG=$(aws ec2 create-security-group \
  --group-name wazuh-agentcore-test \
  --description "Wazuh AgentCore test" \
  --vpc-id <YOUR_VPC_ID> \
  --query 'GroupId' --output text)

# 2. EC2 安全组加入站规则（注意找到 EC2 实际的 SG）
EC2_SG=$(aws ec2 describe-instances --instance-ids <INSTANCE_ID> \
  --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id $EC2_SG \
  --ip-permissions '[{"IpProtocol":"tcp","FromPort":1514,"ToPort":1515,"IpRanges":[{"CidrIp":"172.31.0.0/16"}]}]'

# 3. 部署
export WAZUH_MANAGER_IP=<EC2_PRIVATE_IP>
export VPC_SUBNETS="subnet-aaa,subnet-bbb"
export VPC_SG=$AGENTCORE_SG
./deploy-to-agentcore.sh

# 4. 通过 InvokeAgentRuntimeCommand 初始化 Wazuh
#    (deploy info saved to /tmp/wazuh_agentcore_deploy.json)
SESSION_ID=$(python3 -c "
import boto3, json
client = boto3.client('bedrock-agentcore')
resp = client.invoke_agent_runtime(
    agentRuntimeArn='<RUNTIME_ARN>', qualifier='DEFAULT',
    payload=json.dumps({'cmd': 'echo ready'}))
print(resp['runtimeSessionId'])
")

# 使用 rexec.sh 在容器内执行命令
./rexec.sh $SESSION_ID "sed -i 's|placeholder|<MANAGER_IP>|g' /var/ossec/etc/ossec.conf"
./rexec.sh $SESSION_ID "hostname agentcore-runtime"
./rexec.sh $SESSION_ID "/var/ossec/bin/agent-auth -m <MANAGER_IP> -A agentcore-poc 2>&1"
./rexec.sh $SESSION_ID "/var/ossec/bin/wazuh-control start 2>&1"
./rexec.sh $SESSION_ID "/var/ossec/bin/wazuh-control status"

# 5. 验证 Manager 端
sudo /var/ossec/bin/agent_control -i 001
sudo tail -20 /var/ossec/logs/alerts/alerts.log

# 6. 清理
./cleanup.sh
```

**结果**：Agent Status: Active，收到 rootcheck、SCA、sudo 监控告警。

## VPC 模式配置详解

AgentCore Runtime 的 VPC 模式是 Wazuh Agent 连接外部 Manager 的关键。以下是完整的配置方法和踩坑记录。

### 架构图

```
                       VPC (172.31.0.0/16)
                       │
  ┌────────────────────┼─────────────────────────┐
  │                    │                         │
  │  Private Subnet A (use1-az1)    Private Subnet B (use1-az2)
  │  subnet-aaaa                    subnet-bbbb
  │  Route: 0.0.0.0/0 → NAT GW    Route: 0.0.0.0/0 → NAT GW
  │       │                              │
  │       └──── AgentCore ENI ───────────┘
  │             (Hyperplane, 169.254.x.x)
  │             SG: sg-agentcore (all outbound)
  │                    │
  │                    │ TCP 1514/1515 (VPC 内部路由)
  │                    ▼
  │  Any Subnet (e.g. us-east-1d)
  │  subnet-cccc
  │       │
  │       └──── EC2 (Wazuh Manager)
  │             IP: 172.31.x.x
  │             SG: sg-ec2 (inbound 1514-1515 from VPC CIDR)
  │
  │  Public Subnet
  │  subnet-dddd
  │       └──── NAT Gateway → IGW → Internet
  └──────────────────────────────────────────────┘
```

### 前提条件

| 条件 | 说明 |
|------|------|
| **私有子网 x2** | 至少 2 个不同 AZ，路由表指向 NAT Gateway |
| **NAT Gateway** | 在公有子网，AgentCore 出站互联网流量需要它 |
| **支持的 AZ** | 必须在 AgentCore 支持的 AZ 内（见下表） |
| **不能用公有子网** | AgentCore 官方文档明确说明公有子网不提供互联网连接 |

### 各 Region 支持的 AZ

| Region | AZ ID |
|--------|-------|
| us-east-1 (N. Virginia) | use1-az1, use1-az2, use1-az4 |
| us-east-2 (Ohio) | use2-az1, use2-az2, use2-az3 |
| us-west-2 (Oregon) | usw2-az1, usw2-az2, usw2-az3 |
| eu-west-1 (Ireland) | euw1-az1, euw1-az2, euw1-az3 |

查看子网的 AZ ID：
```bash
aws ec2 describe-subnets --subnet-ids subnet-xxx \
  --query 'Subnets[0].AvailabilityZoneId' --output text
```

### 安全组配置

#### AgentCore Runtime SG（出站）

```bash
# 创建 SG
AGENTCORE_SG=$(aws ec2 create-security-group \
  --group-name agentcore-wazuh \
  --description "AgentCore Runtime for Wazuh test" \
  --vpc-id $VPC_ID --query 'GroupId' --output text)

# 默认的 all-outbound 即可（SG 创建时自带）
# 如果要最小化，至少需要：
#   - TCP 443 出站 (AWS API, ECR, NAT)
#   - TCP 1514-1515 出站到 Wazuh Manager IP
```

#### EC2 (Wazuh Manager) SG（入站）

```bash
# !! 关键坑：一定要找对 EC2 实际绑定的 SG !!
# 不要靠猜，用以下命令确认：
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
EC2_SG=$(aws ec2 describe-instances --instance-ids $INSTANCE_ID \
  --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)
echo "EC2 actual SG: $EC2_SG"

# 添加 Wazuh 端口
aws ec2 authorize-security-group-ingress --group-id $EC2_SG \
  --ip-permissions '[{
    "IpProtocol": "tcp",
    "FromPort": 1514,
    "ToPort": 1515,
    "IpRanges": [{"CidrIp": "172.31.0.0/16", "Description": "Wazuh from VPC"}]
  }]'
```

### AgentCore Runtime 容器内的网络

容器内部看到的网络和普通 EC2/ECS 不同：

```
$ ip addr
eth0: 169.254.0.2/30   ← Service Plane (AgentCore invoke/ping)
eth1: 169.254.1.2/30   ← VPC Plane (你的 VPC 流量走这里)

$ ip route
default via 169.254.1.1 dev eth1   ← VPC 出站走 eth1
169.254.169.254 dev eth0            ← IMDS 走 eth0
```

**注意**：容器的 IP 是 link-local (169.254.x.x)，不是 VPC 子网的 IP。但 VPC 流量经过 Hyperplane 转发后，目标 EC2 看到的源 IP 是 ENI 的 VPC IP（在 172.31.0.0/16 范围内），所以 SG 规则用 VPC CIDR 是对的。

### 配置代码示例

```python
from bedrock_agentcore_starter_toolkit import Runtime

rt = Runtime()
rt.configure(
    entrypoint="wazuh_runtime_agent.py",
    auto_create_execution_role=True,
    auto_create_ecr=True,
    requirements_file="requirements.txt",
    region="us-east-1",
    agent_name="wazuh_runtime_poc",
    deployment_type="container",       # 必须指定 container
    vpc_enabled=True,                  # 开启 VPC 模式
    vpc_subnets=["subnet-aaa", "subnet-bbb"],  # 2个私有子网
    vpc_security_groups=["sg-xxx"],    # AgentCore SG
)
rt.launch(local_build=True)           # 本地构建 Docker → ECR → 部署
```

等效 AWS CLI：
```bash
aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name "wazuh_poc" \
  --network-configuration '{
    "networkMode": "VPC",
    "networkModeConfig": {
      "subnets": ["subnet-aaa", "subnet-bbb"],
      "securityGroups": ["sg-xxx"]
    }
  }' \
  --agent-runtime-artifact '{"containerConfiguration":{"containerUri":"<ECR_URI>"}}'
```

### VPC 配置排查清单

遇到网络不通时按此顺序排查：

| 步骤 | 检查内容 | 命令 |
|------|---------|------|
| 1 | EC2 实际 IP | `hostname -I` 或 `ip -4 addr show \| grep inet` |
| 2 | EC2 **实际** SG（不要猜！） | `aws ec2 describe-instances --instance-ids <ID> --query '..SecurityGroups'` |
| 3 | EC2 SG 入站规则包含 1514-1515 | `aws ec2 describe-security-groups --group-ids <SG>` |
| 4 | 子网在支持的 AZ | `aws ec2 describe-subnets --query 'Subnets[0].AvailabilityZoneId'` |
| 5 | 子网路由表指向 NAT | `aws ec2 describe-route-tables --filters Name=association.subnet-id,Values=<SUBNET>` |
| 6 | NACL 没有阻拦 | `aws ec2 describe-network-acls --filters Name=association.subnet-id,Values=<SUBNET>` |
| 7 | 从容器内测试端口 | `./rexec.sh <SID> "timeout 3 bash -c 'echo > /dev/tcp/<IP>/1515' && echo OK \|\| echo FAIL"` |
| 8 | Wazuh Manager 在监听 | `sudo ss -tlnp \| grep 1514` |

### 我们踩过的坑

1. **找错了 EC2 的 SG** — EC2 有两个 SG（`ssh_web` 和 `ssh-only`），实际绑定的是 `ssh-only`。改了半天 `ssh_web` 完全没用。**教训：永远用 `describe-instances` 确认**
2. **EC2 IP 搞错** — 两台实例的 IP 搞混了（172.31.25.13 vs 172.31.22.104）。**教训：用 `hostname -I` 从 EC2 本机确认**
3. **以为是平台端口限制** — SSH(22) 通但 1515 不通，错误归因为 AgentCore 平台限制。实际是 SG 问题。**教训：先排查 SG，再怀疑平台**
4. **Wazuh Manager Docker 镜像不支持 ARM64** — 官方 `wazuh/wazuh-manager` 只有 x86。ARM64 EC2 要用 apt 安装
5. **hostname 为 localhost 导致注册失败** — 容器默认 hostname 是 `localhost`，Wazuh 会拒绝。用 `hostname agentcore-runtime` 修复
6. **ossec.conf 里留了 placeholder 地址** — 用 `sed -i 's|placeholder|<IP>|g'` 修复
7. **`chmod 750 /var/ossec` 在 setup 阶段过早执行** — 锁权限后 agent-auth 写不了 client.keys。应该先注册再锁权限

## 已知限制

| 限制 | 影响 | 解决方案 |
|------|------|---------|
| `ulimit` 无法提升 (无 CAP_SYS_RESOURCE) | wazuh-modulesd/logcollector 报 ERROR（不影响运行） | `local_internal_options.conf` 降低 rlimit 需求 |
| auditd 不可用 | 无法做 shell 命令级 who-data 监控 | FIM 使用 inotify 模式（功能足够） |
| ARM64 架构 | 需用 aarch64 版 Wazuh 包 | Wazuh 官方 apt 源已提供 arm64 |
| Session 最长 8 小时 | Agent 生命周期有限 | 自动化注册/注销流程 |
| hostname 默认 `localhost` | agent-auth 注册可能失败 | 容器启动时 `hostname agentcore-runtime` |

## 参考文档

- [AgentCore VPC 配置](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-vpc.html)
- [AgentCore Execute Command](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-execute-command.html)
- [Wazuh 用户态安全监控](https://wazuh.com/blog/how-wazuh-provides-endpoint-security-without-kernel-level-access/)
- [Wazuh Docker 部署](https://documentation.wazuh.com/current/deployment-options/docker/wazuh-container.html)
