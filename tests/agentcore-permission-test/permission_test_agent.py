"""
AgentCore Runtime Permission Test Agent v2

Matches the exact test items from the user's previous test screenshot.
"""

import os
import signal
import subprocess
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()


def run_cmd(cmd: str, timeout: int = 5) -> str:
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip() or result.stderr.strip() or "(empty)"
    except subprocess.TimeoutExpired:
        return "(timeout)"
    except Exception as e:
        return f"(error: {e})"


def check_permissions() -> dict:
    r = {}

    # 1. 用户身份
    r["01_id"] = run_cmd("id")
    r["01_whoami"] = run_cmd("whoami")

    # 2. sudo
    r["02_sudo"] = run_cmd("which sudo 2>/dev/null && sudo -n id 2>&1 || echo 'sudo not available'")

    # 3. 访问 /root/
    r["03_access_root_home"] = run_cmd("ls -la /root/ 2>&1")

    # 4. 写系统目录
    r["04_write_usr_bin"] = run_cmd("touch /usr/bin/_test_write 2>&1 && rm /usr/bin/_test_write && echo 'writable' || echo 'read-only'")
    r["04_write_etc"] = run_cmd("touch /etc/_test_write 2>&1 && rm /etc/_test_write && echo 'writable' || echo 'read-only'")

    # 5. apt-get
    r["05_apt_get"] = run_cmd("apt-get update -qq 2>&1 | tail -3")

    # 6. pip install
    r["06_pip_install"] = run_cmd("pip install --dry-run requests 2>&1 | tail -3")

    # 7. 创建用户
    r["07_useradd"] = run_cmd("useradd testuser123 2>&1 || echo 'cannot create user'")

    # 8. Linux Capabilities (CapEff)
    cap_status = run_cmd("cat /proc/self/status | grep ^Cap")
    r["08_capabilities"] = cap_status

    # 9. Signal PID 1
    try:
        os.kill(1, 0)  # signal 0 = check permission only
        r["09_signal_pid1"] = "allowed (signal 0 succeeded)"
    except PermissionError:
        r["09_signal_pid1"] = "denied (PermissionError)"
    except ProcessLookupError:
        r["09_signal_pid1"] = "PID 1 not found"
    except Exception as e:
        r["09_signal_pid1"] = f"error: {e}"

    # 10. dmesg
    r["10_dmesg"] = run_cmd("dmesg 2>&1 | head -5")

    # 11. 进程可见性
    r["11_ps"] = run_cmd("ps aux 2>/dev/null || ps -ef 2>/dev/null")
    r["11_proc_count"] = run_cmd("ls -d /proc/[0-9]* 2>/dev/null | wc -l")

    # 12. 网络
    r["12_network_tcp"] = run_cmd("cat /proc/self/net/tcp 2>/dev/null | head -5")
    r["12_dns"] = run_cmd("getent hosts amazonaws.com 2>/dev/null | head -1")
    r["12_ip"] = run_cmd("ip addr 2>/dev/null | head -15")

    # 13. 文件系统
    r["13_mount"] = run_cmd("mount 2>/dev/null | head -10 || cat /proc/self/mountinfo 2>/dev/null | head -10")
    r["13_df"] = run_cmd("df -hT / 2>/dev/null")

    # 14. inotify (Wazuh FIM)
    r["14_inotify_watches"] = run_cmd("cat /proc/sys/fs/inotify/max_user_watches 2>/dev/null")
    r["14_inotify_instances"] = run_cmd("cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null")

    # 15. auditd
    r["15_auditctl"] = run_cmd("which auditctl 2>/dev/null && auditctl -s 2>&1 || echo 'auditctl not found'")

    # 16. /proc access
    r["16_proc_self"] = str(os.path.exists("/proc/self/status"))
    r["16_proc_1"] = str(os.path.exists("/proc/1"))
    r["16_proc_cgroup"] = run_cmd("cat /proc/self/cgroup 2>/dev/null")

    # 17. System info
    r["17_uname"] = run_cmd("uname -a")
    r["17_hostname"] = run_cmd("hostname")

    return r


@app.entrypoint
async def invoke(payload=None):
    try:
        results = check_permissions()
        lines = ["=== AgentCore Runtime Permission Test v2 ===", ""]
        for key, val in sorted(results.items()):
            label = key.split("_", 1)[1]
            lines.append(f"[{label}] {val}")
            lines.append("")
        summary = "\n".join(lines)
        return {"status": "success", "summary": summary, "details": results}
    except Exception as e:
        return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    app.run()
