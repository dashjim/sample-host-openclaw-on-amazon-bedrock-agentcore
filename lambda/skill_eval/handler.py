"""Skill Eval Lambda — security audit and AI evaluation for user skills.

Supports three actions:
  - audit:  Static security scan (seconds, synchronous)
  - eval:   AI-powered functional + trigger eval via Claude CLI (minutes)
  - scan-all: Enumerate all user namespaces and audit each one

Results stored in DynamoDB (openclaw-identity table) and S3 (HTML reports).
"""

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Configuration ---
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")
IDENTITY_TABLE_NAME = os.environ.get("IDENTITY_TABLE_NAME", "openclaw-identity")
S3_USER_FILES_BUCKET = os.environ.get("S3_USER_FILES_BUCKET", "")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")

# --- AWS Clients ---
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
identity_table = dynamodb.Table(IDENTITY_TABLE_NAME)
s3_client = boto3.client(
    "s3", region_name=AWS_REGION,
    config=boto3.session.Config(signature_version="s3v4"),
)

# Max skills to scan per user (safety limit)
MAX_SKILLS_PER_USER = 50
# Max time for full eval per skill (seconds)
EVAL_TIMEOUT_PER_SKILL = 300


def _download_user_skills(namespace, work_dir):
    """Download user's .openclaw/skills/ from S3 to local directory.

    Returns list of skill directory paths.
    """
    prefix = f"{namespace}/.openclaw/skills/"
    skills_dir = Path(work_dir) / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    paginator = s3_client.get_paginator("list_objects_v2")
    downloaded_files = 0

    for page in paginator.paginate(Bucket=S3_USER_FILES_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel_path = key[len(prefix):]
            if not rel_path or obj.get("Size", 0) == 0:
                continue

            local_path = skills_dir / rel_path
            local_path.parent.mkdir(parents=True, exist_ok=True)

            s3_client.download_file(S3_USER_FILES_BUCKET, key, str(local_path))
            downloaded_files += 1

            if downloaded_files > 5000:  # Safety limit
                logger.warning("Too many files for namespace %s, stopping", namespace)
                break

    # Discover skill directories (directories containing SKILL.md)
    skill_dirs = []
    for skill_md in skills_dir.rglob("SKILL.md"):
        skill_dirs.append(str(skill_md.parent))
        if len(skill_dirs) >= MAX_SKILLS_PER_USER:
            break

    return skill_dirs


def _run_audit(skill_path):
    """Run skill-eval audit on a single skill directory. Returns dict."""
    try:
        from skill_eval.cli import run_audit
        report = run_audit(skill_path, verbose=False, include_all=True)

        findings = []
        for f in report.findings:
            findings.append({
                "code": f.code,
                "severity": f.severity.value if hasattr(f.severity, "value") else str(f.severity),
                "message": f.title,
                "detail": f.detail,
                "file": str(f.file_path) if f.file_path else None,
                "line": f.line_number,
            })

        return {
            "score": report.score,
            "grade": report.grade,
            "criticals": sum(1 for f in findings if f["severity"] == "CRITICAL"),
            "warnings": sum(1 for f in findings if f["severity"] == "WARNING"),
            "infos": sum(1 for f in findings if f["severity"] == "INFO"),
            "findings": findings,
        }
    except Exception as e:
        logger.exception("Audit failed for %s", skill_path)
        return {
            "score": 0,
            "grade": "F",
            "criticals": 1,
            "warnings": 0,
            "infos": 0,
            "findings": [{"code": "ERR-001", "severity": "CRITICAL",
                          "message": f"Audit error: {e}", "file": None, "line": None}],
            "error": str(e),
        }


def _run_eval(skill_path, timeout=EVAL_TIMEOUT_PER_SKILL):
    """Run skill-eval report (audit + functional + trigger) on a single skill.

    Requires Claude CLI with CLAUDE_CODE_USE_BEDROCK=1.
    Returns dict with unified score.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["skill-eval", "report", skill_path, "--format", "json"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
        else:
            logger.error("skill-eval report failed: %s", result.stderr[:500])
            return {"error": result.stderr[:500], "score": 0, "grade": "F"}
    except subprocess.TimeoutExpired:
        return {"error": f"Eval timed out after {timeout}s", "score": 0, "grade": "F"}
    except Exception as e:
        logger.exception("Eval failed for %s", skill_path)
        return {"error": str(e), "score": 0, "grade": "F"}


def _save_scan_result(namespace, user_id, result, scan_type="audit"):
    """Save scan result to DynamoDB under the user's record."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    timestamp = str(int(time.time()))

    # Compute aggregate score
    skills = result.get("skills", [])
    avg_score = int(sum(s.get("score", 0) for s in skills) / max(len(skills), 1))
    total_criticals = sum(s.get("criticals", 0) for s in skills)

    if avg_score >= 90:
        overall_grade = "A"
    elif avg_score >= 80:
        overall_grade = "B"
    elif avg_score >= 70:
        overall_grade = "C"
    elif avg_score >= 60:
        overall_grade = "D"
    else:
        overall_grade = "F"

    record = {
        "PK": f"USER#{user_id}" if user_id else f"SCAN#{namespace}",
        "SK": f"SKILLSCAN#latest",
        "scanType": scan_type,
        "score": avg_score,
        "grade": overall_grade,
        "totalSkills": len(skills),
        "totalCriticals": total_criticals,
        "skills": skills,
        "scannedAt": now_iso,
        "namespace": namespace,
    }

    try:
        identity_table.put_item(Item=record)
        # Also save timestamped history
        history_record = {**record, "SK": f"SKILLSCAN#{timestamp}"}
        identity_table.put_item(Item=history_record)
    except ClientError as e:
        logger.error("Failed to save scan result: %s", e)


def _upload_html_report(namespace, skill_name, html_content):
    """Upload HTML report to S3 for viewing in Admin UI."""
    timestamp = int(time.time())
    key = f"{namespace}/_skill-eval/{skill_name}_report_{timestamp}.html"
    try:
        s3_client.put_object(
            Bucket=S3_USER_FILES_BUCKET, Key=key,
            Body=html_content.encode("utf-8"),
            ContentType="text/html",
        )
        return key
    except ClientError as e:
        logger.error("Failed to upload HTML report: %s", e)
        return None


def _resolve_user_id(namespace):
    """Look up userId from namespace via CHANNEL# records."""
    channel_key = namespace.replace("_", ":", 1)  # telegram_123 → telegram:123
    try:
        resp = identity_table.get_item(
            Key={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE"}
        )
        item = resp.get("Item")
        if item:
            return item.get("userId", "")
    except ClientError:
        pass
    return ""


def _list_all_namespaces():
    """List all user namespaces from S3."""
    namespaces = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_USER_FILES_BUCKET, Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            ns = prefix["Prefix"].rstrip("/")
            namespaces.append(ns)
    return namespaces


def handle_audit(namespace):
    """Run static audit on all skills for a user namespace."""
    with tempfile.TemporaryDirectory() as work_dir:
        skill_dirs = _download_user_skills(namespace, work_dir)

        if not skill_dirs:
            return {
                "namespace": namespace,
                "skills": [],
                "message": "No skills found",
            }

        results = []
        for skill_path in skill_dirs:
            skill_name = Path(skill_path).name
            audit_result = _run_audit(skill_path)
            audit_result["name"] = skill_name

            # Generate and upload HTML report
            try:
                from skill_eval.html_report import generate_html_report
                report_data = {
                    "skill_name": skill_name,
                    "skill_path": skill_path,
                    "score": audit_result.get("score", 0),
                    "grade": audit_result.get("grade", "F"),
                    "findings": audit_result.get("findings", []),
                }
                html = generate_html_report(report_data)
                s3_key = _upload_html_report(namespace, skill_name, html)
                if s3_key:
                    audit_result["reportKey"] = s3_key
            except Exception as e:
                logger.warning("HTML report generation failed for %s: %s", skill_name, e)

            results.append(audit_result)

        result = {"namespace": namespace, "skills": results}

        # Save to DynamoDB
        user_id = _resolve_user_id(namespace)
        _save_scan_result(namespace, user_id, result, scan_type="audit")

        return result


def handle_eval(namespace):
    """Run full AI evaluation (audit + functional + trigger) for a user namespace."""
    with tempfile.TemporaryDirectory() as work_dir:
        skill_dirs = _download_user_skills(namespace, work_dir)

        if not skill_dirs:
            return {
                "namespace": namespace,
                "skills": [],
                "message": "No skills found",
            }

        results = []
        for skill_path in skill_dirs:
            skill_name = Path(skill_path).name

            # First run audit (always works)
            audit_result = _run_audit(skill_path)

            # Then try full eval (needs Claude CLI)
            eval_result = _run_eval(skill_path)

            combined = {
                "name": skill_name,
                "audit": audit_result,
                "eval": eval_result,
                "score": eval_result.get("score", audit_result.get("score", 0)),
                "grade": eval_result.get("grade", audit_result.get("grade", "F")),
                "criticals": audit_result.get("criticals", 0),
                "warnings": audit_result.get("warnings", 0),
            }
            results.append(combined)

        result = {"namespace": namespace, "skills": results}
        user_id = _resolve_user_id(namespace)
        _save_scan_result(namespace, user_id, result, scan_type="eval")

        return result


def handle_scan_all():
    """Scan all user namespaces (used by EventBridge scheduled trigger)."""
    namespaces = _list_all_namespaces()
    summary = {"scanned": 0, "skipped": 0, "errors": 0, "results": []}

    for ns in namespaces:
        # Skip system prefixes
        if ns.startswith("_") or ns.startswith("."):
            summary["skipped"] += 1
            continue

        try:
            result = handle_audit(ns)
            summary["results"].append({
                "namespace": ns,
                "totalSkills": len(result.get("skills", [])),
                "grade": _compute_grade(result.get("skills", [])),
            })
            summary["scanned"] += 1
        except Exception as e:
            logger.exception("Failed to scan namespace %s", ns)
            summary["errors"] += 1
            summary["results"].append({
                "namespace": ns,
                "error": str(e),
            })

    return summary


def _compute_grade(skills):
    if not skills:
        return "-"
    avg = sum(s.get("score", 0) for s in skills) / len(skills)
    if avg >= 90:
        return "A"
    if avg >= 80:
        return "B"
    if avg >= 70:
        return "C"
    if avg >= 60:
        return "D"
    return "F"


def lambda_handler(event, context):
    """Lambda entry point.

    Invoked by:
      - Admin Lambda (via boto3 lambda.invoke) with action + namespace
      - EventBridge Scheduler with action: scan-all
    """
    action = event.get("action", "audit")
    namespace = event.get("namespace", "")

    logger.info("skill-eval action=%s namespace=%s", action, namespace)

    if action == "audit":
        if not namespace:
            return {"statusCode": 400, "error": "namespace required"}
        result = handle_audit(namespace)
        return {"statusCode": 200, "body": result}

    elif action == "eval":
        if not namespace:
            return {"statusCode": 400, "error": "namespace required"}
        result = handle_eval(namespace)
        return {"statusCode": 200, "body": result}

    elif action == "scan-all":
        result = handle_scan_all()
        return {"statusCode": 200, "body": result}

    else:
        return {"statusCode": 400, "error": f"Unknown action: {action}"}
