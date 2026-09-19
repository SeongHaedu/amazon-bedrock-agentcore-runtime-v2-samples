# common.py
# Shared configuration and helpers for every script.
#
# All settings come from environment variables. Account ids, role ARNs, ECR URIs and bucket
# names are never hardcoded in this repository. Required values fail loudly at runtime.
import json
import os
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# First public release that carries the platformVersion field. Earlier versions raise
# ParamValidationError before the request is sent.
MIN_BOTO3 = "1.43.95"

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
SETUP_STATE_PATH = RESULTS_DIR / "setup_prerequisites.json"


def _setup_state():
    """Read back what scripts/setup_prerequisites.py recorded.

    A process cannot change its parent shell's environment, so that script writes the Region,
    role ARN, container URI and bucket name to a file instead of expecting the reader to
    export them by hand. An explicit environment variable always wins over the file, and the
    file simply does not exist before the script has run.
    """
    try:
        return json.loads(SETUP_STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


_STATE = _setup_state()
_STATE_ROLE = _STATE.get("role") or {}
_STATE_ECR = _STATE.get("ecr") or {}
_STATE_S3 = _STATE.get("s3") or {}

REGION = os.environ.get("AWS_REGION") or _STATE.get("region") or "us-west-2"
PROFILE = os.environ.get("AWS_PROFILE") or None

ROLE_ARN = os.environ.get("AGENTCORE_ROLE_ARN") or _STATE_ROLE.get("arn")
CONTAINER_URI = os.environ.get("AGENTCORE_CONTAINER_URI") or _STATE_ECR.get("container_uri")
S3_BUCKET = os.environ.get("AGENTCORE_S3_BUCKET") or _STATE_S3.get("name")
S3_PREFIX = os.environ.get("AGENTCORE_S3_PREFIX", "agentcore/codezip/agent.zip")
CODE_RUNTIME = os.environ.get("AGENTCORE_CODE_RUNTIME", "PYTHON_3_11")
# entryPoint is an array. Split on commas so that more than one element can be passed,
# for example AGENTCORE_ENTRY_POINT="opentelemetry-instrument,main.py".
ENTRY_POINT = [p.strip() for p in os.environ.get("AGENTCORE_ENTRY_POINT", "main.py").split(",") if p.strip()]

# Prefix applied to every runtime name this repository creates. cleanup.py deletes
# only runtimes carrying this prefix, which keeps it from touching unrelated resources.
#
# The default is applied with `or` rather than os.environ.get's default argument: exporting
# AGENTCORE_NAME_PREFIX="" puts the key in the environment, so the default would not apply.
# An empty prefix makes "any name".startswith("") true and would match every runtime in the
# account and Region. cleanup.py also enforces a minimum length.
NAME_PREFIX = os.environ.get("AGENTCORE_NAME_PREFIX") or "v2sample_"

# Minimum prefix length required for cleanup. A short prefix deletes too much.
MIN_NAME_PREFIX_LEN = 4

# Tag applied to everything scripts/setup_prerequisites.py creates. scripts/cleanup.py reads
# it back before deleting, so it can never remove a role, repository or bucket that already
# existed in the account.
MANAGED_TAG_KEY = "ManagedBy"
MANAGED_TAG_VALUE = "agentcore-runtime-v2-samples"

# Names of the prerequisite resources. An environment variable wins, then what a previous run
# of setup_prerequisites.py recorded, then the default.
SETUP_ROLE_NAME = (
    os.environ.get("AGENTCORE_SETUP_ROLE_NAME")
    or _STATE_ROLE.get("name")
    or "AgentCoreV2SamplesExecutionRole"
)
SETUP_ROLE_POLICY_NAME = "AgentCoreV2SamplesExecutionPolicy"
SETUP_ECR_REPOSITORY = (
    os.environ.get("AGENTCORE_SETUP_ECR_REPOSITORY")
    or _STATE_ECR.get("name")
    or "agentcore-v2-samples"
)


def setup_bucket_name(account_id):
    """Bucket names are globally unique, so the account id and Region are part of the default."""
    return (
        os.environ.get("AGENTCORE_SETUP_S3_BUCKET")
        or _STATE_S3.get("name")
        or f"agentcore-v2-samples-{account_id}-{REGION}"
    )


NETWORK = {"networkMode": "PUBLIC"}


def parse_env_extra(raw):
    """Parse AGENTCORE_ENV_EXTRA ("KEY=VALUE,KEY2=VALUE2") into a dict.

    Values containing a comma are not supported; pass those through the agent code or a
    separate mechanism instead. An entry without "=" is a configuration mistake and fails
    here rather than silently dropping the value.
    """
    if not raw:
        return {}
    extra = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise SystemExit(
                f"AGENTCORE_ENV_EXTRA entry is not KEY=VALUE: {item!r}"
            )
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"AGENTCORE_ENV_EXTRA entry has an empty key: {item!r}")
        extra[key] = value.strip()
    return extra


def agent_env_vars():
    """Environment variables handed to the agent.

    PYTHONUNBUFFERED is always set so that the [MODULE_START] / [ENTRYPOINT_REACHED] markers
    reach CloudWatch Logs immediately. Anything else comes from the caller:

      BEDROCK_MODEL_ID       forwarded under the same name when set. agent-bench reads it.
                             Regions without a us.-prefixed cross-Region inference profile
                             (ap-northeast-1, for example) need a Region-local profile here.
      AGENTCORE_ENV_EXTRA    "KEY=VALUE,KEY2=VALUE2" for anything else the agent reads.

    V2 caps the total size of these variables at 1.5 KB for direct code deployments and
    2.5 KB for container agents, against 4 KB on V1.
    """
    env = {"PYTHONUNBUFFERED": "1"}
    env.update(parse_env_extra(os.environ.get("AGENTCORE_ENV_EXTRA")))
    model_id = os.environ.get("BEDROCK_MODEL_ID")
    if model_id:
        env["BEDROCK_MODEL_ID"] = model_id
    return env


# Evaluated once at import time so that every script in a single run sees the same set.
ENV_VARS = agent_env_vars()

# A V2 create or update takes minutes because the snapshot is prepared. A timeout sized for
# V1 would give up before the call completes, so keep plenty of headroom.
TIMEOUT_SEC = int(os.environ.get("AGENTCORE_WAIT_TIMEOUT_SEC", "1800"))
INTERVAL_SEC = 5


def require_env(name, value, hint):
    if not value:
        raise SystemExit(f"Environment variable {name} is not set. {hint}")
    return value


def make_client(service):
    session = boto3.Session(profile_name=PROFILE) if PROFILE else boto3.Session()
    return session.client(service, region_name=REGION)


def artifact_for(kind):
    """platformVersion is specified the same way for container and codezip deployments.
    Only the artifact differs."""
    if kind == "container":
        require_env(
            "AGENTCORE_CONTAINER_URI",
            CONTAINER_URI,
            "Set it to <account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>.",
        )
        return {"containerConfiguration": {"containerUri": CONTAINER_URI}}
    if kind == "codezip":
        require_env(
            "AGENTCORE_S3_BUCKET",
            S3_BUCKET,
            "Set the S3 bucket that scripts/setup_codezip_artifact.py uploads to.",
        )
        return {
            "codeConfiguration": {
                "code": {"s3": {"bucket": S3_BUCKET, "prefix": S3_PREFIX}},
                "runtime": CODE_RUNTIME,
                "entryPoint": ENTRY_POINT,
            }
        }
    raise SystemExit(f"Unknown artifact kind: {kind} (use container or codezip)")


def platform_version_supported():
    """Report whether the installed botocore service model carries platformVersion.
    The service model is checked rather than the version string because a build with a
    newer version number can still lack the field."""
    import botocore.session

    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    return {
        # create / update carry platformVersion on the request; get carries it on the
        # response. Its absence from the GetAgentRuntime input is the correct behavior.
        "create_input": "platformVersion" in model.operation_model("CreateAgentRuntime").input_shape.members,
        "update_input": "platformVersion" in model.operation_model("UpdateAgentRuntime").input_shape.members,
        "get_output": "platformVersion" in model.operation_model("GetAgentRuntime").output_shape.members,
    }


def wait_until_ready(client, agent_runtime_id, timeout_sec=TIMEOUT_SEC, interval_sec=INTERVAL_SEC):
    """Terminal state is status == "READY" or status.endswith("FAILED").

    Failure statuses are matched by suffix rather than enumerated: a failure status missing
    from the list would leave this loop spinning forever. AgentCore Runtime ships no waiter,
    so this poll is what confirms a terminal state before the next call.
    """
    start = time.monotonic()
    transitions = []
    last = None
    while True:
        status = client.get_agent_runtime(agentRuntimeId=agent_runtime_id)["status"]
        if status != last:
            elapsed = time.monotonic() - start
            transitions.append({"status": status, "at_sec": round(elapsed, 1)})
            print(f"    [{elapsed:7.1f}s] {status}", flush=True)
            last = status
        if status == "READY" or status.endswith("FAILED"):
            return status, transitions
        if time.monotonic() - start > timeout_sec:
            return f"TIMEOUT(last={status})", transitions
        time.sleep(interval_sec)


def wait_until_deleted(client, agent_runtime_id, timeout_sec=600, interval_sec=5):
    """Deletion is complete on ResourceNotFoundException or DELETE_FAILED.

    Waiting only for ResourceNotFoundException would spin forever once a delete ends in
    DELETE_FAILED. wait_until_ready cannot be reused for deletion.
    """
    start = time.monotonic()
    while True:
        try:
            status = client.get_agent_runtime(agentRuntimeId=agent_runtime_id)["status"]
        except ClientError as error:
            if error.response["Error"]["Code"] == "ResourceNotFoundException":
                return "DELETED", round(time.monotonic() - start, 1)
            raise
        if status == "DELETE_FAILED":
            return "DELETE_FAILED", round(time.monotonic() - start, 1)
        if time.monotonic() - start > timeout_sec:
            return f"TIMEOUT(last={status})", round(time.monotonic() - start, 1)
        time.sleep(interval_sec)


def describe_platform_version(client, agent_runtime_id):
    """Call get_agent_runtime and record the presence of the platformVersion key separately
    from its value. Write your own checks as resp.get("platformVersion", "V1")."""
    resp = client.get_agent_runtime(agentRuntimeId=agent_runtime_id)
    return {
        "platform_version": resp.get("platformVersion"),
        "key_present": "platformVersion" in resp,
        "status": resp.get("status"),
        "agent_runtime_version": resp.get("agentRuntimeVersion"),
        "raw_response": jsonable(resp),
    }


def list_sample_runtimes(client):
    """List only the runtimes carrying NAME_PREFIX. list_agent_runtimes does not return
    platformVersion, so call get_agent_runtime when the value is needed."""
    paginator = client.get_paginator("list_agent_runtimes")
    found = []
    for page in paginator.paginate():
        for runtime in page.get("agentRuntimes", []):
            if runtime["agentRuntimeName"].startswith(NAME_PREFIX):
                found.append(runtime)
    return found


def jsonable(obj):
    """createdAt / lastUpdatedAt are datetimes that json.dumps cannot serialize as they are.
    Unreadable objects fall back to repr so that nothing is dropped from the record."""
    from datetime import date, datetime

    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


def save_result(filename, obj):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / filename
    path.write_text(json.dumps(jsonable(obj), indent=2, ensure_ascii=False, default=str))
    print(f"saved: {path}", flush=True)
    return path
