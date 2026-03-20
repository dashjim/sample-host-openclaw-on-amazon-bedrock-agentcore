#!/bin/bash
# Build and deploy the admin UI to S3 + CloudFront.
#
# Usage:
#   ./scripts/deploy-admin-ui.sh
#
# This script:
#   1. Reads configuration from CloudFormation outputs
#   2. Builds the admin UI with Vite
#   3. Syncs the build to S3
#   4. Invalidates CloudFront cache
#
# Prerequisites:
#   - CDK stack deployed (OpenClawAdmin)
#   - Node.js installed (for npm build)
#   - aws cli configured with appropriate permissions
#
# Environment:
#   CDK_DEFAULT_REGION — AWS region (default: us-west-2)
#   AWS_PROFILE        — AWS CLI profile (optional)

set -euo pipefail

REGION="${CDK_DEFAULT_REGION:-${AWS_REGION:-us-west-2}}"
PROFILE_ARG=""
if [ -n "${AWS_PROFILE:-}" ]; then
    PROFILE_ARG="--profile $AWS_PROFILE"
fi

echo "=== OpenClaw Admin UI Deploy ==="
echo ""
echo "Reading configuration from CloudFormation..."

API_URL=$(aws cloudformation describe-stacks --stack-name OpenClawAdmin \
    --query "Stacks[0].Outputs[?OutputKey=='AdminApiUrl'].OutputValue" \
    --output text --region "$REGION" $PROFILE_ARG)
POOL_ID=$(aws cloudformation describe-stacks --stack-name OpenClawAdmin \
    --query "Stacks[0].Outputs[?OutputKey=='AdminUserPoolId'].OutputValue" \
    --output text --region "$REGION" $PROFILE_ARG)
CLIENT_ID=$(aws cloudformation describe-stacks --stack-name OpenClawAdmin \
    --query "Stacks[0].Outputs[?OutputKey=='AdminClientId'].OutputValue" \
    --output text --region "$REGION" $PROFILE_ARG)
BUCKET=$(aws cloudformation describe-stacks --stack-name OpenClawAdmin \
    --query "Stacks[0].Outputs[?OutputKey=='AdminFrontendBucketName'].OutputValue" \
    --output text --region "$REGION" $PROFILE_ARG)
CF_DIST_ID=$(aws cloudformation describe-stacks --stack-name OpenClawAdmin \
    --query "Stacks[0].Outputs[?OutputKey=='AdminDistributionId'].OutputValue" \
    --output text --region "$REGION" $PROFILE_ARG)

echo "API URL: $API_URL"
echo "Cognito Pool: $POOL_ID"
echo "S3 Bucket: $BUCKET"
echo "CloudFront Distribution: $CF_DIST_ID"
echo ""

# Build
cd "$(dirname "$0")/../admin-ui"
echo "Installing dependencies..."
npm ci --silent

echo "Building..."
VITE_API_URL="$API_URL" \
VITE_COGNITO_USER_POOL_ID="$POOL_ID" \
VITE_COGNITO_CLIENT_ID="$CLIENT_ID" \
VITE_COGNITO_REGION="$REGION" \
npm run build

# Deploy
echo "Syncing to S3..."
aws s3 sync dist/ "s3://$BUCKET/" --delete --region "$REGION" $PROFILE_ARG

echo "Invalidating CloudFront..."
aws cloudfront create-invalidation --distribution-id "$CF_DIST_ID" --paths "/*" $PROFILE_ARG --output text

ADMIN_URL=$(aws cloudformation describe-stacks --stack-name OpenClawAdmin \
    --query "Stacks[0].Outputs[?OutputKey=='AdminUrl'].OutputValue" \
    --output text --region "$REGION" $PROFILE_ARG)

echo ""
echo "=== Deploy complete ==="
echo ""
echo "Admin UI deployed!"
echo "URL: $ADMIN_URL"
echo ""
