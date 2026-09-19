# create_runtime.py
# platformVersion を指定して (または省略して) エージェントランタイムを作成し、
# READY までの所要時間を計測する。V1 と V2 の作成コストの差がそのまま数値で出る。
#
# usage:
#   python scripts/create_runtime.py <suffix> <container|codezip> [V1|V2|omit]
#
# example:
#   python scripts/create_runtime.py basic_v2 container V2      # V2 を明示して作成する
#   python scripts/create_runtime.py basic_v1 container omit    # 省略して作成する (V1 になる)
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

    # 計時は API 呼び出しの前から始める。ポーリング開始以降しか測らないと、
    # API 呼び出し自体の時間が落ちてしまうためである。
    t0 = time.monotonic()
    try:
        created = client.create_agent_runtime(**kwargs)
    except ParamValidationError as error:
        # 送信前に botocore が拒否した場合。boto3 が platformVersion 未対応のバージョンだと
        # ここに落ちる。scripts/check_sdk_version.py を先に実行して切り分ける。
        return {"ok": False, "error_type": "ParamValidationError", "message": str(error)}
    except ClientError as error:
        return {
            "ok": False,
            "error_type": error.response["Error"]["Code"],
            "message": error.response["Error"]["Message"],
        }

    api_sec = time.monotonic() - t0
    print(f"    create 応答: status={created['status']} ({api_sec:.2f}s)", flush=True)

    status, transitions = wait_until_ready(client, created["agentRuntimeId"])
    record = {
        "ok": status == "READY",
        "name": name,
        "requested_platform_version": platform_version,
        "agentRuntimeId": created["agentRuntimeId"],
        "agentRuntimeArn": created["agentRuntimeArn"],
        "agentRuntimeVersion": created["agentRuntimeVersion"],
        # create のレスポンスに platformVersion は含まれない。値の確認は get_agent_runtime で行う。
        "platform_version_in_create_response": "platformVersion" in created,
        "create_response_raw": jsonable(created),
        "create_api_call_sec": round(api_sec, 2),
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
        "AgentCore Runtime の実行ロール ARN を設定する。",
    )

    name = f"{NAME_PREFIX}{suffix}"
    artifact = artifact_for(kind)
    client = make_client("bedrock-agentcore-control")

    print(
        f"[{name}] create_agent_runtime kind={kind} platformVersion={platform_version}",
        flush=True,
    )
    if platform_version == "V2":
        print("    V2 はスナップショット準備のため READY まで数分かかる。", flush=True)

    record = timed_create(client, name, artifact, platform_version)
    if record.get("ok"):
        got = record["get_after_ready"]
        print(
            f"[{name}] READY in {record['total_sec']}s  id={record['agentRuntimeId']}  "
            f"platformVersion={got['platform_version']!r} (key present: {got['key_present']})",
            flush=True,
        )
    else:
        print(f"[{name}] 失敗: {record.get('error_type')} {record.get('message', '')}", flush=True)

    save_result(f"create_{suffix}.json", record)
    if not record.get("ok"):
        sys.exit(1)


if __name__ == "__main__":
    main()
