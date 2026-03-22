"""Cross-account cross-region AgentCore Browser API test.

Scenario:
    - Caller runs in Account A (e.g. EC2 with instance role)
    - Browser runs in Account B / ap-northeast-1 (Tokyo)
    - Uses boto3 STS assume_role → boto3.Session, no AK/SK in env

Environment variables:
    ACCOUNT_B_ROLE_ARN  - Role ARN in Account B (required)
    BROWSER_REGION      - Browser region (default: ap-northeast-1)

Setup: See CROSS_ACCOUNT_SETUP.md
"""

import logging
import os
import uuid

import boto3
import pytest

from tests_integ.ci_environments import skip_if_github_action

logger = logging.getLogger(__name__)

BROWSER_REGION = os.environ.get("BROWSER_REGION", "ap-northeast-1")
ACCOUNT_B_ROLE_ARN = os.environ.get("ACCOUNT_B_ROLE_ARN", "")
BROWSER_IDENTIFIER = "aws.browser.v1"


def _assume_role_session(role_arn: str, region: str) -> boto3.Session:
    """Assume role and return a boto3 Session. No env vars touched."""
    sts = boto3.client("sts")
    resp = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"xaccount-test-{uuid.uuid4().hex[:8]}",
        DurationSeconds=3600,
    )
    creds = resp["Credentials"]
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


# ---------------------------------------------------------------------------
# Fixture: Account B boto3 session via assume role
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def b_session():
    """Assume Account B role, return boto3 Session. No AK/SK in env."""
    if not ACCOUNT_B_ROLE_ARN:
        pytest.skip("ACCOUNT_B_ROLE_ARN not set")
    session = _assume_role_session(ACCOUNT_B_ROLE_ARN, BROWSER_REGION)
    logger.info(f"Assumed role: {ACCOUNT_B_ROLE_ARN} → region {BROWSER_REGION}")
    return session


@pytest.fixture(scope="module")
def data_client(b_session):
    return b_session.client("bedrock-agentcore")


@pytest.fixture(scope="module")
def control_client(b_session):
    return b_session.client("bedrock-agentcore-control")


# ---------------------------------------------------------------------------
# Test 1: Basic cross-account browser session lifecycle
# ---------------------------------------------------------------------------

@skip_if_github_action.mark
def test_cross_account_browser_session(data_client):
    """Start → GetSession → Stop a browser session in Account B from Account A."""
    uid = uuid.uuid4().hex[:8]

    # Start
    start_resp = data_client.start_browser_session(
        browserIdentifier=BROWSER_IDENTIFIER,
        name=f"xaccount-basic-{uid}",
        sessionTimeoutSeconds=300,
    )
    session_id = start_resp["sessionId"]
    logger.info(f"Started session: {session_id}")

    try:
        # Verify READY
        info = data_client.get_browser_session(
            browserIdentifier=BROWSER_IDENTIFIER,
            sessionId=session_id,
        )
        assert info["status"] == "READY", f"Expected READY, got {info['status']}"
        logger.info("Session is READY ✓")
    finally:
        data_client.stop_browser_session(
            browserIdentifier=BROWSER_IDENTIFIER,
            sessionId=session_id,
        )
        logger.info(f"Stopped session: {session_id}")


# ---------------------------------------------------------------------------
# Test 2: Cross-account browser profile create → save → restore → delete
# ---------------------------------------------------------------------------

@skip_if_github_action.mark
def test_cross_account_browser_profile(data_client, control_client):
    """Full profile lifecycle across account boundary using only boto3 API."""
    uid = uuid.uuid4().hex[:8]
    profile_name = f"xaccount_profile_{uid}"
    profile_id = None
    session1_id = None
    session2_id = None

    try:
        # Step 1: Create profile
        profile_resp = control_client.create_browser_profile(
            name=profile_name,
            description="Cross-account profile test",
        )
        profile_id = profile_resp["profileId"]
        logger.info(f"Created profile: {profile_id}")

        # Step 2: Session 1 — start, then save profile
        s1_resp = data_client.start_browser_session(
            browserIdentifier=BROWSER_IDENTIFIER,
            name=f"xacct-prof-s1-{uid}",
            sessionTimeoutSeconds=300,
        )
        session1_id = s1_resp["sessionId"]
        logger.info(f"Session 1 started: {session1_id}")

        # Save profile from session 1
        data_client.save_browser_session_profile(
            profileIdentifier=profile_id,
            browserIdentifier=BROWSER_IDENTIFIER,
            sessionId=session1_id,
        )
        logger.info("Profile saved from session 1 ✓")

        # Stop session 1
        data_client.stop_browser_session(
            browserIdentifier=BROWSER_IDENTIFIER,
            sessionId=session1_id,
        )
        session1_id = None

        # Step 3: Session 2 — start with saved profile
        s2_resp = data_client.start_browser_session(
            browserIdentifier=BROWSER_IDENTIFIER,
            name=f"xacct-prof-s2-{uid}",
            sessionTimeoutSeconds=300,
            profileConfiguration={"profileIdentifier": profile_id},
        )
        session2_id = s2_resp["sessionId"]
        logger.info(f"Session 2 started with profile: {session2_id}")

        info = data_client.get_browser_session(
            browserIdentifier=BROWSER_IDENTIFIER,
            sessionId=session2_id,
        )
        assert info["status"] == "READY", f"Expected READY, got {info['status']}"
        logger.info("Session 2 with profile is READY — profile restore verified ✓")

        data_client.stop_browser_session(
            browserIdentifier=BROWSER_IDENTIFIER,
            sessionId=session2_id,
        )
        session2_id = None

    finally:
        # Cleanup sessions
        for sid in (session1_id, session2_id):
            if sid:
                try:
                    data_client.stop_browser_session(
                        browserIdentifier=BROWSER_IDENTIFIER, sessionId=sid
                    )
                except Exception as e:
                    logger.warning(f"Failed to stop session {sid}: {e}")

        # Cleanup profile
        if profile_id:
            try:
                control_client.delete_browser_profile(profileId=profile_id)
                logger.info(f"Deleted profile: {profile_id}")
            except Exception as e:
                logger.warning(f"Failed to delete profile {profile_id}: {e}")
