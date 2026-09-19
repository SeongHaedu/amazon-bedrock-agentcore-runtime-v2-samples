# setup_prerequisites.py
# Create the three resources the other scripts expect: the AgentCore Runtime execution role,
# an ECR repository for the container path, and an S3 bucket for the direct code deployment
# path. Prints the environment variables to export when it finishes.
#
# Everything created here carries the ManagedBy tag, which scripts/cleanup.py checks before
# deleting. A resource that already exists is left as it is and reported as "exists".
#
# The execution role follows the policy documented at
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html
#
# usage:
#   python scripts/setup_prerequisites.py
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from botocore.exceptions import ClientError

from common import (
    MANAGED_TAG_KEY,
    MANAGED_TAG_VALUE,
    REGION,
    RESULTS_DIR,
    SETUP_ECR_REPOSITORY,
    SETUP_ROLE_NAME,
    SETUP_ROLE_POLICY_NAME,
    make_client,
    save_result,
    setup_bucket_name,
)

# Tag the other steps build and deploy. It only has to be consistent within this repository.
CONTAINER_TAG = "v2sample"


def trust_policy(account_id):
    """Only AgentCore Runtime may assume the role, and only on behalf of this account."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AssumeRolePolicy",
                "Effect": "Allow",
                "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                "Action": "sts:AssumeRole",
                # The Region is a wildcard because the role is global: running this script in a
                # second Region reuses the same role rather than creating another one.
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account_id},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:*:{account_id}:*"},
                },
            }
        ],
    }


def permission_policy(account_id):
    """Permissions for the steps in this repository.

    Log access is what benchmark/build_breakdown.py reads. Bedrock access is what agent-bench
    needs. ECR access is what the container path needs. X-Ray and CloudWatch metrics are part
    of the documented policy. Reading the ZIP from S3 is not included: the service fetches the
    artifact itself.
    """
    # Region wildcards for the same reason as the trust policy: one role serves every Region
    # this repository is run in.
    log_group = f"arn:aws:logs:*:{account_id}:log-group"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ECRImageAccess",
                "Effect": "Allow",
                "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
                "Resource": [f"arn:aws:ecr:*:{account_id}:repository/*"],
            },
            {
                "Sid": "ECRTokenAccess",
                "Effect": "Allow",
                "Action": ["ecr:GetAuthorizationToken"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["logs:DescribeLogStreams", "logs:CreateLogGroup"],
                "Resource": [f"{log_group}:/aws/bedrock-agentcore/runtimes/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["logs:PutResourcePolicy"],
                "Resource": [f"{log_group}:/aws/bedrock-agentcore/runtimes/*"],
            },
            {
                "Effect": "Allow",
                "Action": ["logs:DescribeLogGroups"],
                "Resource": [f"{log_group}:*"],
            },
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": [f"{log_group}:/aws/bedrock-agentcore/runtimes/*:log-stream:*"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                "Resource": ["*"],
            },
            {
                "Effect": "Allow",
                "Action": "cloudwatch:PutMetricData",
                "Resource": "*",
                "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
            },
            {
                "Sid": "GetAgentAccessToken",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                ],
                "Resource": [
                    f"arn:aws:bedrock-agentcore:*:{account_id}:workload-identity-directory/default",
                    f"arn:aws:bedrock-agentcore:*:{account_id}:workload-identity-directory/default/workload-identity/*",
                ],
            },
            {
                "Sid": "BedrockModelInvocation",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": [
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:*:{account_id}:*",
                ],
            },
        ],
    }


def ensure_role(iam, account_id):
    try:
        created = iam.create_role(
            RoleName=SETUP_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust_policy(account_id)),
            Description="Execution role for the AgentCore Runtime V2 platformVersion samples",
            Tags=[{"Key": MANAGED_TAG_KEY, "Value": MANAGED_TAG_VALUE}],
        )
        state = "created"
        arn = created["Role"]["Arn"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        state = "exists"
        arn = iam.get_role(RoleName=SETUP_ROLE_NAME)["Role"]["Arn"]
        # Write the trust policy back as well. A role left by an earlier run can carry a
        # Region-specific aws:SourceArn, and create_agent_runtime in another Region then fails
        # with "Role validation failed ... its trust policy allows assumption by this service".
        iam.update_assume_role_policy(
            RoleName=SETUP_ROLE_NAME, PolicyDocument=json.dumps(trust_policy(account_id))
        )

    # The inline policy is written on every run so that a policy update reaches an existing
    # role as well.
    iam.put_role_policy(
        RoleName=SETUP_ROLE_NAME,
        PolicyName=SETUP_ROLE_POLICY_NAME,
        PolicyDocument=json.dumps(permission_policy(account_id)),
    )
    print(f"  role       {state:8s} {arn}", flush=True)
    return {"state": state, "name": SETUP_ROLE_NAME, "arn": arn}


def ensure_repository(ecr):
    try:
        created = ecr.create_repository(
            repositoryName=SETUP_ECR_REPOSITORY,
            tags=[{"Key": MANAGED_TAG_KEY, "Value": MANAGED_TAG_VALUE}],
        )
        state = "created"
        uri = created["repository"]["repositoryUri"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "RepositoryAlreadyExistsException":
            raise
        state = "exists"
        uri = ecr.describe_repositories(repositoryNames=[SETUP_ECR_REPOSITORY])["repositories"][0][
            "repositoryUri"
        ]
    print(f"  ecr        {state:8s} {uri}", flush=True)
    # container_uri carries the tag the other scripts push to and deploy from, so that
    # common.py can use it as is.
    return {
        "state": state,
        "name": SETUP_ECR_REPOSITORY,
        "uri": uri,
        "container_uri": f"{uri}:{CONTAINER_TAG}",
    }


def ensure_bucket(s3, bucket):
    kwargs = {"Bucket": bucket}
    # us-east-1 rejects an explicit LocationConstraint.
    if REGION != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": REGION}
    try:
        s3.create_bucket(**kwargs)
        state = "created"
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            raise
        state = "exists"

    s3.put_bucket_tagging(
        Bucket=bucket,
        Tagging={"TagSet": [{"Key": MANAGED_TAG_KEY, "Value": MANAGED_TAG_VALUE}]},
    )
    print(f"  s3         {state:8s} s3://{bucket}", flush=True)
    return {"state": state, "name": bucket}


def main():
    account_id = make_client("sts").get_caller_identity()["Account"]
    bucket = setup_bucket_name(account_id)

    print(f"account={account_id} region={REGION}", flush=True)
    role = ensure_role(make_client("iam"), account_id)
    repository = ensure_repository(make_client("ecr"))
    bucket_result = ensure_bucket(make_client("s3"), bucket)

    # The Python scripts read these from results/setup_prerequisites.json, so nothing has to be
    # exported for them. The env file exists for the docker commands in Step 2, which do need
    # the values in the shell.
    env_path = RESULTS_DIR / "setup_prerequisites.env"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        f"export AWS_REGION={REGION}\n"
        f"export AGENTCORE_ROLE_ARN={role['arn']}\n"
        f"export AGENTCORE_CONTAINER_URI={repository['container_uri']}\n"
        f"export AGENTCORE_S3_BUCKET={bucket_result['name']}\n"
    )
    print(f"\nwrote: {env_path}", flush=True)
    print("  The Python scripts read the saved values on their own.", flush=True)
    print("  For the docker commands: source results/setup_prerequisites.env", flush=True)

    save_result(
        "setup_prerequisites.json",
        {
            "account_id": account_id,
            "region": REGION,
            "role": role,
            "ecr": repository,
            "s3": bucket_result,
            "tag": {MANAGED_TAG_KEY: MANAGED_TAG_VALUE},
        },
    )


if __name__ == "__main__":
    main()
