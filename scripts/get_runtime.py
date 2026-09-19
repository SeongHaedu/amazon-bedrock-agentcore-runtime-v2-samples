# get_runtime.py
# ランタイムの platformVersion を確認する。
#
# list_agent_runtimes のレスポンスには platformVersion が含まれないため、値を見るには
# get_agent_runtime を個別に呼ぶ必要がある。
#
# usage:
#   python scripts/get_runtime.py                      # プレフィックス一致のランタイムを全件
#   python scripts/get_runtime.py <agentRuntimeId> ...  # 指定した ID のみ
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
        print("対象のランタイムが見つからなかった。", flush=True)
        return

    results = {}
    for label, runtime_id in targets:
        info = describe_platform_version(client, runtime_id)
        results[label] = {"agent_runtime_id": runtime_id, **info}
        # platformVersion を一度も明示指定していないランタイムではキー自体が返らない場合がある。
        # 判定コードは resp.get("platformVersion", "V1") の形で書く。
        print(
            f"[{label}] status={info['status']} "
            f"platformVersion={info['platform_version']!r} (key present: {info['key_present']}) "
            f"agentRuntimeVersion={info['agent_runtime_version']}",
            flush=True,
        )

    save_result("get_runtime.json", results)


if __name__ == "__main__":
    main()
