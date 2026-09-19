# create_runtime.py
# Create an agent runtime with platformVersion set (or omitted) and time how long it takes
# to reach READY. The difference in creation cost between V1 and V2 comes out as a number.
#
# usage:
#   python scripts/create_runtime.py <suffix> <container|codezip> [V1|V2|omit]
#
# example:
#   python scripts/create_runtime.py basic_v2 container V2      # create with V2 set
#   python scripts/create_runtime.py basic_v1 container omit    # create with it omitted (V1)
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from botocore.exceptions import ClientError, ParamValidationError

from common import (
    ENV_VARS,
    NAME_PREFIX,
    NETWORK,
    ROLE_ARN,
    artifact_for,
    describe_platform_version,
    jsonable,
    make_client,
    require_env,
    save_result,
    wait_until_ready,
)

USAGE = "usage: python scripts/create_runtime.py <suffix> <container|codezip> [V1|V2|omit]"


def timed_create(client, name, artifact, platform_version):
    kwargs = dict(
        agentRuntimeName=name,
        agentRuntimeArtifact=artifact,
        roleArn=ROLE_ARN,
        networkConfiguration=NETWORK,
        environmentVariables=ENV_VARS,
    )
    if platform_version != "omit":
        kwargs["platformVersion"] = platform_version

    # Start timing before the API call. Timing only from the first poll would drop the
    # duration of the call itself.
    t0 = time.monotonic()
    try:
        created = client.create_agent_runtime(**kwargs)
    except ParamValidationError as error:
        # botocore rejected the request before sending it, which is what happens when the
        # installed boto3 has no platformVersion field. Run scripts/check_sdk_version.py.
        return {"ok": False, "error_type": "ParamValidationError", "message": str(error)}
    except ClientError as error:
        return {
            "ok": False,
            "error_type": error.response["Error"]["Code"],
            "message": error.response["Error"]["Message"],
        }

    api_sec = time.monotonic() - t0
    print(f"    create response: status={created['status']} ({api_sec:.2f}s)", flush=True)

    status, transitions = wait_until_ready(client, created["agentRuntimeId"])
    record = {
        "ok": status == "READY",
        "name": name,
        "requested_platform_version": platform_version,
        "agentRuntimeId": created["agentRuntimeId"],
        "agentRuntimeArn": created["agentRuntimeArn"],
        "agentRuntimeVersion": created["agentRuntimeVersion"],
        # The create response does not carry platformVersion. Confirm the value with
        # get_agent_runtime.
        "platform_version_in_create_response": "platformVersion" in created,
        "create_response_raw": jsonable(created),
        "create_api_call_sec": round(api_sec, 2),
        "environment_variables": sorted(ENV_VARS),
        "final_status": status,
        "total_sec": round(time.monotonic() - t0, 1),
        "transitions": transitions,
    }
    if status == "READY":
        record["get_after_ready"] = describe_platform_version(client, created["agentRuntimeId"])
    return record


def main():
    if len(sys.argv) < 3:
        raise SystemExit(USAGE)
    suffix, kind = sys.argv[1], sys.argv[2]
    platform_version = sys.argv[3] if len(sys.argv) > 3 else "V2"
    if platform_version not in ("V1", "V2", "omit"):
        raise SystemExit(USAGE)

    require_env(
        "AGENTCORE_ROLE_ARN",
        ROLE_ARN,
        "Set the execution role ARN for AgentCore Runtime.",
    )

    name = f"{NAME_PREFIX}{suffix}"
    artifact = artifact_for(kind)
    client = make_client("bedrock-agentcore-control")

    print(
        f"[{name}] create_agent_runtime kind={kind} platformVersion={platform_version}",
        flush=True,
    )
    if platform_version == "V2":
        print("    V2 takes minutes to reach READY because the snapshot is prepared.", flush=True)

    record = timed_create(client, name, artifact, platform_version)
    if record.get("ok"):
        got = record["get_after_ready"]
        print(
            f"[{name}] READY in {record['total_sec']}s  id={record['agentRuntimeId']}  "
            f"platformVersion={got['platform_version']!r} (key present: {got['key_present']})",
            flush=True,
        )
    else:
        # The timeout path carries no error_type; final_status holds TIMEOUT(last=...).
        # Print the cause on either path.
        reason = record.get("error_type") or record.get("final_status")
        print(f"[{name}] failed: {reason} {record.get('message', '')}", flush=True)
        if str(reason).startswith("TIMEOUT"):
            print(
                "    Raise AGENTCORE_WAIT_TIMEOUT_SEC and check again."
                " A V2 create takes minutes because the snapshot is prepared.",
                flush=True,
            )

    save_result(f"create_{suffix}.json", record)
    if not record.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
