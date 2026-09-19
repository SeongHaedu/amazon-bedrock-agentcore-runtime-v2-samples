# cleanup_runtimes.py
# Delete only the runtimes whose name starts with AGENTCORE_NAME_PREFIX (default "v2sample_").
#
# Four safeguards:
#   1. The minimum prefix length is checked at runtime. An empty prefix matches every runtime,
#      which would make the other two safeguards meaningless.
#   2. The prefix is matched both when listing and inside the loop.
#   3. Without --yes the script prints the targets and exits without deleting.
#   4. Deletion is complete on ResourceNotFoundException or DELETE_FAILED, not just the former.
#
# usage:
#   python scripts/cleanup_runtimes.py          # print the targets only
#   python scripts/cleanup_runtimes.py --yes    # actually delete
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from botocore.exceptions import ClientError

from common import (
    MIN_NAME_PREFIX_LEN,
    NAME_PREFIX,
    list_sample_runtimes,
    make_client,
    save_result,
    wait_until_deleted,
)


def main():
    # An empty or very short prefix would match runtimes that were never meant to be touched.
    # Fail before calling AWS.
    if len(NAME_PREFIX) < MIN_NAME_PREFIX_LEN:
        raise SystemExit(
            f"AGENTCORE_NAME_PREFIX is too short ({NAME_PREFIX!r})."
            f" At least {MIN_NAME_PREFIX_LEN} characters are required to prevent accidental deletion."
        )

    apply_delete = "--yes" in sys.argv[1:]
    client = make_client("bedrock-agentcore-control")
    candidates = list_sample_runtimes(client)

    if not candidates:
        print(f"No runtime matches the prefix {NAME_PREFIX!r}.", flush=True)
        save_result("cleanup.json", {"applied": apply_delete, "targets": []})
        return

    print(f"Deletion targets (prefix {NAME_PREFIX!r}):", flush=True)
    for runtime in candidates:
        print(
            f"  - {runtime['agentRuntimeName']} ({runtime['agentRuntimeId']}) "
            f"status={runtime['status']}",
            flush=True,
        )

    if not apply_delete:
        print("\nRan without --yes, so nothing was deleted. Re-run with --yes to delete.", flush=True)
        save_result(
            "cleanup.json",
            {"applied": False, "targets": [r["agentRuntimeName"] for r in candidates]},
        )
        return

    results = []
    for runtime in candidates:
        name = runtime["agentRuntimeName"]
        runtime_id = runtime["agentRuntimeId"]
        if not name.startswith(NAME_PREFIX):
            print(f"[skip] {name} does not match the prefix", flush=True)
            continue

        print(f"[{name}] delete_agent_runtime id={runtime_id}", flush=True)
        try:
            client.delete_agent_runtime(agentRuntimeId=runtime_id)
        except ClientError as error:
            results.append(
                {
                    "name": name,
                    "agent_runtime_id": runtime_id,
                    "ok": False,
                    "error_type": error.response["Error"]["Code"],
                    "message": error.response["Error"]["Message"],
                }
            )
            print(f"[{name}] delete request failed: {error.response['Error']['Code']}", flush=True)
            continue

        final_status, elapsed_sec = wait_until_deleted(client, runtime_id)
        results.append(
            {
                "name": name,
                "agent_runtime_id": runtime_id,
                "ok": final_status == "DELETED",
                "final_status": final_status,
                "elapsed_sec": elapsed_sec,
            }
        )
        print(f"[{name}] {final_status} ({elapsed_sec}s)", flush=True)

    save_result("cleanup.json", {"applied": True, "targets": results})
    if not all(r.get("ok") for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
