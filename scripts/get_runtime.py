# get_runtime.py
# Report the platformVersion of a runtime.
#
# list_agent_runtimes does not return platformVersion, so reading the value means calling
# get_agent_runtime per runtime.
#
# usage:
#   python scripts/get_runtime.py                       # every runtime matching the prefix
#   python scripts/get_runtime.py <agentRuntimeId> ...  # only the ids given
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import describe_platform_version, list_sample_runtimes, make_client, save_result


def main():
    client = make_client("bedrock-agentcore-control")

    if len(sys.argv) > 1:
        targets = [(runtime_id, runtime_id) for runtime_id in sys.argv[1:]]
    else:
        targets = [(r["agentRuntimeName"], r["agentRuntimeId"]) for r in list_sample_runtimes(client)]

    if not targets:
        print("No matching runtime found.", flush=True)
        return

    results = {}
    for label, runtime_id in targets:
        info = describe_platform_version(client, runtime_id)
        results[label] = {"agent_runtime_id": runtime_id, **info}
        # The key itself can be absent on a runtime that never had platformVersion set
        # explicitly. Write your own checks as resp.get("platformVersion", "V1").
        print(
            f"[{label}] status={info['status']} "
            f"platformVersion={info['platform_version']!r} (key present: {info['key_present']}) "
            f"agentRuntimeVersion={info['agent_runtime_version']}",
            flush=True,
        )

    save_result("get_runtime.json", results)


if __name__ == "__main__":
    main()
