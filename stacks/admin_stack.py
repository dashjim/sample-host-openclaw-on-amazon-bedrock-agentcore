"""Admin Stack — Control Plane for OpenClaw.

Deploys a serverless admin UI (React SPA on S3/CloudFront) with a Python Lambda
backend behind API Gateway HTTP API, authenticated via a dedicated Cognito User Pool.
Provides channel management, user/allowlist management, and per-user S3 file browsing.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_ecr_assets as ecr_assets,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
)
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_auth
import cdk_nag
from constructs import Construct

from stacks import retention_days


class AdminStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        identity_table_name: str,
        identity_table_arn: str,
        s3_user_files_bucket_name: str,
        cmk_arn: str,
        router_api_url: str,
        telegram_secret_name: str,
        slack_secret_name: str,
        feishu_secret_name: str,
        webhook_secret_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        lambda_timeout = int(self.node.try_get_context("admin_lambda_timeout_seconds") or "60")
        lambda_memory = int(self.node.try_get_context("admin_lambda_memory_mb") or "256")

        # ---- Cognito User Pool (Admin) ----
        self.user_pool = cognito.UserPool(
            self,
            "AdminUserPool",
            user_pool_name="openclaw-admin-pool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=False,
                require_uppercase=False,
                require_digits=False,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.user_pool_client = self.user_pool.add_client(
            "AdminClient",
            user_pool_client_name="openclaw-admin-client",
            auth_flows=cognito.AuthFlow(
                user_password=True,
                admin_user_password=True,
            ),
            generate_secret=False,
        )

        # ---- S3 Bucket (Frontend) ----
        self.frontend_bucket = s3.Bucket(
            self,
            "AdminFrontendBucket",
            bucket_name=f"openclaw-admin-frontend-{account}-{region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            enforce_ssl=True,
        )

        # ---- CloudFront Distribution ----
        oac = cloudfront.CfnOriginAccessControl(
            self,
            "AdminOAC",
            origin_access_control_config=cloudfront.CfnOriginAccessControl.OriginAccessControlConfigProperty(
                name="openclaw-admin-oac",
                origin_access_control_origin_type="s3",
                signing_behavior="always",
                signing_protocol="sigv4",
            ),
        )

        self.distribution = cloudfront.Distribution(
            self,
            "AdminDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(
                    self.frontend_bucket,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
            ],
        )

        # ---- CloudWatch Log Groups ----
        admin_log_group = logs.LogGroup(
            self,
            "AdminLogGroup",
            log_group_name="/openclaw/lambda/admin",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        access_log_group = logs.LogGroup(
            self,
            "AdminApiAccessLogGroup",
            log_group_name="/openclaw/apigw/admin",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---- Lambda Function ----
        self.admin_fn = _lambda.Function(
            self,
            "AdminApiFn",
            function_name="openclaw-admin-api",
            runtime=_lambda.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=_lambda.Code.from_asset("lambda/admin"),
            timeout=Duration.seconds(lambda_timeout),
            memory_size=lambda_memory,
            environment={
                "IDENTITY_TABLE_NAME": identity_table_name,
                "S3_USER_FILES_BUCKET": s3_user_files_bucket_name,
                "WEBHOOK_SECRET_ID": webhook_secret_name,
                "TELEGRAM_SECRET_ID": telegram_secret_name,
                "SLACK_SECRET_ID": slack_secret_name,
                "FEISHU_SECRET_ID": feishu_secret_name,
                "ROUTER_API_URL": router_api_url,
            },
            log_group=admin_log_group,
        )

        # ---- Lambda IAM Permissions ----

        # DynamoDB
        self.admin_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:Scan",
                    "dynamodb:Query",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:DeleteItem",
                ],
                resources=[identity_table_arn],
            )
        )

        # Secrets Manager — channel secrets (read-write)
        self.admin_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/channels/*",
                ],
            )
        )

        # Secrets Manager — webhook secret (read-only)
        self.admin_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/webhook-secret*",
                ],
            )
        )

        # S3 — user files bucket
        self.admin_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[f"arn:aws:s3:::{s3_user_files_bucket_name}"],
            )
        )
        self.admin_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:DeleteObject"],
                resources=[f"arn:aws:s3:::{s3_user_files_bucket_name}/*"],
            )
        )

        # EventBridge Scheduler — delete schedules on user deletion
        self.admin_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["scheduler:DeleteSchedule"],
                resources=[
                    f"arn:aws:scheduler:{region}:{account}:schedule/openclaw-cron/*",
                ],
            )
        )

        # KMS
        self.admin_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[cmk_arn],
            )
        )

        # ---- Skill Eval Lambda (container image) ----
        skill_eval_timeout = int(self.node.try_get_context("skill_eval_lambda_timeout_seconds") or "900")
        skill_eval_memory = int(self.node.try_get_context("skill_eval_lambda_memory_mb") or "1024")
        default_model_id = self.node.try_get_context("default_model_id") or "us.anthropic.claude-sonnet-4-6"
        skill_eval_schedule = self.node.try_get_context("skill_eval_schedule") or "rate(1 day)"
        skill_eval_enabled = str(self.node.try_get_context("skill_eval_enabled") or "true").lower() == "true"

        skill_eval_log_group = logs.LogGroup(
            self,
            "SkillEvalLogGroup",
            log_group_name="/openclaw/lambda/skill-eval",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.skill_eval_fn = _lambda.DockerImageFunction(
            self,
            "SkillEvalFn",
            function_name="openclaw-skill-eval",
            code=_lambda.DockerImageCode.from_image_asset(
                "lambda/skill_eval",
                platform=ecr_assets.Platform.LINUX_ARM64,
            ),
            timeout=Duration.seconds(skill_eval_timeout),
            memory_size=skill_eval_memory,
            architecture=_lambda.Architecture.ARM_64,
            environment={
                "IDENTITY_TABLE_NAME": identity_table_name,
                "S3_USER_FILES_BUCKET": s3_user_files_bucket_name,
                "BEDROCK_MODEL_ID": default_model_id,
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_REGION_OVERRIDE": region,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "us.anthropic.claude-sonnet-4-6",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "us.anthropic.claude-opus-4-6-v1",
            },
            log_group=skill_eval_log_group,
        )

        # Skill Eval IAM — DynamoDB
        self.skill_eval_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"],
                resources=[identity_table_arn],
            )
        )
        # Skill Eval IAM — S3 (read skills + write reports)
        self.skill_eval_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[f"arn:aws:s3:::{s3_user_files_bucket_name}"],
            )
        )
        self.skill_eval_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject"],
                resources=[f"arn:aws:s3:::{s3_user_files_bucket_name}/*"],
            )
        )
        # Skill Eval IAM — Bedrock (for Claude CLI functional/trigger eval)
        self.skill_eval_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:ListInferenceProfiles",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/*",
                    "arn:aws:bedrock:*::inference-profile/*",
                ],
            )
        )
        # Skill Eval IAM — KMS
        self.skill_eval_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[cmk_arn],
            )
        )

        # Admin Lambda — permission to invoke skill-eval Lambda
        self.admin_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[self.skill_eval_fn.function_arn],
            )
        )
        self.admin_fn.add_environment("SKILL_EVAL_FUNCTION_NAME", self.skill_eval_fn.function_name)

        # ---- EventBridge Schedule (daily skill scan) ----
        if skill_eval_enabled:
            scan_rule = events.Rule(
                self,
                "SkillEvalScheduleRule",
                rule_name="openclaw-skill-eval-daily",
                schedule=events.Schedule.expression(skill_eval_schedule),
                description="Daily skill security scan for all users",
            )
            scan_rule.add_target(
                events_targets.LambdaFunction(
                    self.skill_eval_fn,
                    event=events.RuleTargetInput.from_object({"action": "scan-all"}),
                )
            )

        # ---- API Gateway HTTP API ----
        cf_domain = self.distribution.distribution_domain_name

        lambda_integration = apigwv2_integrations.HttpLambdaIntegration(
            "AdminLambdaIntegration",
            handler=self.admin_fn,
        )

        jwt_authorizer = apigwv2_auth.HttpJwtAuthorizer(
            "AdminJwtAuthorizer",
            jwt_issuer=f"https://cognito-idp.{region}.amazonaws.com/{self.user_pool.user_pool_id}",
            jwt_audience=[self.user_pool_client.user_pool_client_id],
        )

        self.http_api = apigwv2.HttpApi(
            self,
            "AdminApi",
            api_name="openclaw-admin-api",
            description="OpenClaw Admin Control Plane API",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=[f"https://{cf_domain}"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.PUT,
                    apigwv2.CorsHttpMethod.DELETE,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Authorization", "Content-Type"],
                max_age=Duration.hours(1),
            ),
        )

        # Catch-all route — single path minimizes Lambda resource-based policy size
        # (each route × method adds a permission; 20KB limit reached at ~50 permissions)
        self.http_api.add_routes(
            path="/api/{proxy+}",
            methods=[
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.POST,
                apigwv2.HttpMethod.PUT,
                apigwv2.HttpMethod.DELETE,
            ],
            integration=lambda_integration,
            authorizer=jwt_authorizer,
        )

        # Access logging on default stage
        default_stage = self.http_api.default_stage
        if default_stage:
            cfn_stage = default_stage.node.default_child
            cfn_stage.access_log_settings = apigwv2.CfnStage.AccessLogSettingsProperty(
                destination_arn=access_log_group.log_group_arn,
                format='{"requestId":"$context.requestId","ip":"$context.identity.sourceIp",'
                '"method":"$context.httpMethod","path":"$context.path",'
                '"status":"$context.status","latency":"$context.responseLatency"}',
            )

        # Grant API Gateway permission to write to access log group
        access_log_group.grant_write(iam.ServicePrincipal("apigateway.amazonaws.com"))

        # ---- Outputs ----
        CfnOutput(self, "AdminUserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "AdminClientId", value=self.user_pool_client.user_pool_client_id)
        CfnOutput(self, "AdminApiUrl", value=self.http_api.url or "")
        CfnOutput(self, "AdminFrontendBucketName", value=self.frontend_bucket.bucket_name)
        CfnOutput(self, "AdminDistributionId", value=self.distribution.distribution_id)
        CfnOutput(self, "AdminUrl", value=f"https://{cf_domain}")

        # ---- cdk-nag suppressions ----
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.admin_fn,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="Lambda basic execution role is AWS-recommended for CloudWatch Logs.",
                    applies_to=[
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Admin Lambda needs wildcard access: Secrets Manager scoped to "
                    "openclaw/channels/* and openclaw/webhook-secret*. S3 scoped to user "
                    "files bucket. Scheduler scoped to openclaw-cron/* group.",
                    applies_to=[
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/channels/*",
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/webhook-secret*",
                        f"Resource::arn:aws:s3:::{s3_user_files_bucket_name}/*",
                        f"Resource::arn:aws:scheduler:{region}:{account}:schedule/openclaw-cron/*",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="Python 3.13 is the latest stable runtime supported in all regions.",
                ),
            ],
            apply_to_children=True,
        )

        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.skill_eval_fn,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="Lambda basic execution role is AWS-recommended for CloudWatch Logs.",
                    applies_to=[
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Skill eval Lambda needs: S3 wildcard scoped to user files bucket, "
                    "Bedrock wildcard for cross-region inference profiles.",
                    applies_to=[
                        f"Resource::arn:aws:s3:::{s3_user_files_bucket_name}/*",
                        "Resource::arn:aws:bedrock:*::foundation-model/*",
                        f"Resource::arn:aws:bedrock:{region}:{account}:inference-profile/*",
                        "Resource::arn:aws:bedrock:*::inference-profile/*",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="Container image Lambda does not use managed runtimes.",
                ),
            ],
            apply_to_children=True,
        )

        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.user_pool,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-COG1",
                    reason="Password policy uses 12-char minimum. Complexity requirements "
                    "relaxed since admin accounts are manually created by operators.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-COG2",
                    reason="MFA is OPTIONAL (TOTP available). Mandatory MFA not enforced "
                    "to reduce friction for single-admin deployments.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-COG3",
                    reason="Advanced security features (WAF, compromised credentials) not "
                    "needed for admin-only pool with manual user creation.",
                ),
            ],
            apply_to_children=True,
        )

        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.distribution,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR1",
                    reason="Geo restrictions not needed for admin panel.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR2",
                    reason="WAF not needed for static SPA admin panel.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR3",
                    reason="Access logging not needed for low-traffic admin panel. "
                    "API access logging is enabled separately.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR4",
                    reason="Using default CloudFront certificate. Custom domain not required.",
                ),
            ],
            apply_to_children=True,
        )

        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.frontend_bucket,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason="Server access logging not needed for static SPA assets bucket.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-S10",
                    reason="SSL enforced via enforce_ssl=True on the bucket.",
                ),
            ],
            apply_to_children=True,
        )

        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.http_api,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-APIG1",
                    reason="Access logging is configured on the default stage.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-APIG4",
                    reason="JWT authorizer (Cognito) is configured on all /api/* routes.",
                ),
            ],
            apply_to_children=True,
        )
