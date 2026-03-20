#!/bin/bash
# Create an admin user in the admin control plane Cognito User Pool.
#
# Usage:
#   ./scripts/setup-admin.sh <email>
#
# This script:
#   1. Creates a new admin user in the Cognito User Pool
#   2. Generates a temporary password
#   3. Displays the CloudFront admin URL
#
# Prerequisites:
#   - CDK stack deployed (OpenClawAdmin)
#   - aws cli configured with appropriate permissions
#
# Environment:
#   CDK_DEFAULT_REGION — AWS region (default: us-west-2)
#   AWS_PROFILE        — AWS CLI profile (optional)

set -euo pipefail

EMAIL="${1:-}"
if [ -z "$EMAIL" ]; then
    echo "Usage: $0 <email>"
    exit 1
fi

REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
PROFILE_ARG=""
if [ -n "${AWS_PROFILE:-}" ]; then
    PROFILE_ARG="--profile $AWS_PROFILE"
fi

echo "=== OpenClaw Admin User Setup ==="
echo ""

# Get admin user pool ID from CloudFormation
echo "Fetching admin user pool ID..."
POOL_ID=$(aws cloudformation describe-stacks \
    --stack-name OpenClawAdmin \
    --query "Stacks[0].Outputs[?OutputKey=='AdminUserPoolId'].OutputValue" \
    --output text --region "$REGION" $PROFILE_ARG)

if [ -z "$POOL_ID" ] || [ "$POOL_ID" = "None" ]; then
    echo "ERROR: Could not find AdminUserPoolId. Is OpenClawAdmin stack deployed?"
    exit 1
fi

echo "User Pool ID: $POOL_ID"
echo ""

# Generate temporary password (16 chars with at least one uppercase and digit)
TEMP_PASSWORD=$(openssl rand -base64 16 | tr -d '/+=' | head -c 16)
TEMP_PASSWORD="${TEMP_PASSWORD}A1!"

echo "Creating admin user..."
aws cognito-idp admin-create-user \
    --user-pool-id "$POOL_ID" \
    --username "$EMAIL" \
    --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
    --temporary-password "$TEMP_PASSWORD" \
    --region "$REGION" $PROFILE_ARG

echo ""
echo "=== Setup complete ==="
echo ""
echo "Admin user created: $EMAIL"
echo "Temporary password: $TEMP_PASSWORD"
echo ""
echo "Login at the CloudFront URL and change your password on first login."
echo ""

# Show CloudFront URL
ADMIN_URL=$(aws cloudformation describe-stacks \
    --stack-name OpenClawAdmin \
    --query "Stacks[0].Outputs[?OutputKey=='AdminUrl'].OutputValue" \
    --output text --region "$REGION" $PROFILE_ARG 2>/dev/null || echo "(not available)")
echo "Admin URL: $ADMIN_URL"
echo ""
