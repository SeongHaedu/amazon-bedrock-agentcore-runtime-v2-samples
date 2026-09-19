# switch_platform_version.py
# Move an existing runtime between V1 and V2 with update_agent_runtime and time READY.
#
# V1 -> V2 takes minutes because the snapshot is prepared. V2 -> V1 completes in seconds; no
# preparation is needed. Omitting platformVersion on a runtime that is already V2 keeps it on
# V2 and still prepares a snapshot, so ordinary updates such as swapping the artifact also
# take minutes on V2.
#
# usage:
#   python scripts/switch_platform_version.py <agentRuntimeId> <V1|V2|omit>
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from botocore.exceptions import ClientError, ParamValidationError

from common import describe_platform_version, jsonable, make_client, save_result, wait_until_ready

USAGE = "usage: python scripts/switch_platform_version.py <agentRuntimeId> <V1|V2|omit>"


def main():
    if len(sys.argv) < 3:
        raise SystemExit(USAGE)
    runtime_id, platform_version = sys.argv[1], sys.argv[2]
    if platform_version not in ("V1", "V2", "omit"):
        raise SystemExit(USAGE)

    client = make_client("bedrock-agentcore-control")

    # update_agent_runtime requires roleArn and agentRuntimeArtifact, so read the current
    # values with get and pass them through. environmentVariables is carried forward
    # explicitly because the documentation does not state whether omitting it clears the
    # existing values, and losing them silently would change the conditions.
    current = client.get_agent_runtime(agentRuntimeId=runtime_id)
    if current["status"] not in ("READY",) and not current["status"].endswith("FAILED"):
        raise SystemExit(
            f"Target is not in a terminal state (status={current['status']})."
            " Calling update during CREATING / UPDATING / DELETING returns ConflictException."
        )

    kwargs = dict(
        agentRuntimeId=runtime_id,
        roleArn=current["roleArn"],
        networkConfiguration=current["networkConfiguration"],
        agentRuntimeArtifact=current["agentRuntimeArtifact"],
        environmentVariables=current.get("environmentVariables", {}),
    )
    if platform_version != "omit":
        kwargs["platformVersion"] = platform_version

    record = {
        "agent_runtime_id": runtime_id,
        "requested_platform_version": platform_version,
        "platform_version_before": current.get("platformVersion"),
        "platform_version_key_present_before": "platformVersion" in current,
        "agent_runtime_version_before": current["agentRuntimeVersion"],
    }

    t0 = time.monotonic()
    try:
        updated = client.update_agent_runtime(**kwargs)
    except ParamValidationError as error:
        record.update(result="param_validation_error", error=str(error))
        print(f"botocore rejected the request before sending it: {error}", flush=True)
    except ClientError as error:
        record.update(
            result="client_error",
            error_code=error.response["Error"]["Code"],
            error_message=error.response["Error"]["Message"],
            api_latency_sec=round(time.monotonic() - t0, 2),
        )
        print(f"{record['error_code']}: {record['error_message']}", flush=True)
    else:
        record["api_latency_sec"] = round(time.monotonic() - t0, 2)
        record["status_immediate"] = updated["status"]
        # The update response does not carry platformVersion either.
        record["platform_version_in_update_response"] = "platformVersion" in updated
        record["update_response_raw"] = jsonable(updated)
        print(f"status: {updated['status']} (api {record['api_latency_sec']}s)", flush=True)

        status, transitions = wait_until_ready(client, runtime_id)
        after = describe_platform_version(client, runtime_id)
        record.update(
            result="ok",
            final_status=status,
            transitions=transitions,
            ready_elapsed_sec=round(time.monotonic() - t0, 1),
            platform_version_after=after["platform_version"],
            platform_version_key_present_after=after["key_present"],
            agent_runtime_version_after=after["agent_runtime_version"],
        )
        print(
            f"{status} in {record['ready_elapsed_sec']}s  "
            f"platformVersion: {record['platform_version_before']!r} -> "
            f"{record['platform_version_after']!r}",
            flush=True,
        )

    save_result(f"switch_{runtime_id}_{platform_version}.json", record)
    if record.get("result") != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()
