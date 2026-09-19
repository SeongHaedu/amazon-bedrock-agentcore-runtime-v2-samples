# cleanup_runtimes.py
# AGENTCORE_NAME_PREFIX (既定 "v2sample_") で始まるランタイムのみを削除する。
#
# 安全策は 4 点である。
#   1. プレフィックスの長さの下限を実行時に検証する。空文字列は全ランタイムに一致するため、
#      これが無いと以降の 2 つの安全策が意味を持たなくなる。
#   2. プレフィックスの一致を列挙時とループ内の両方で検証する。
#   3. --yes を付けない場合は対象一覧の表示だけで終了し、削除しない。
#   4. 削除の完了判定は ResourceNotFoundException と DELETE_FAILED の両方を見る。
#
# usage:
#   python scripts/cleanup_runtimes.py          # 対象を表示するだけ
#   python scripts/cleanup_runtimes.py --yes    # 実際に削除する
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
    # 空文字列や極端に短いプレフィックスでは、意図しないランタイムまで一致してしまう。
    # AWS を呼ぶ前に落とす。
    if len(NAME_PREFIX) < MIN_NAME_PREFIX_LEN:
        raise SystemExit(
            f"AGENTCORE_NAME_PREFIX が短すぎる ({NAME_PREFIX!r})。"
            f" 誤削除を防ぐため {MIN_NAME_PREFIX_LEN} 文字以上を要求する。"
        )

    apply_delete = "--yes" in sys.argv[1:]
    client = make_client("bedrock-agentcore-control")
    candidates = list_sample_runtimes(client)

    if not candidates:
        print(f"プレフィックス {NAME_PREFIX!r} に一致するランタイムは見つからなかった。", flush=True)
        save_result("cleanup.json", {"applied": apply_delete, "targets": []})
        return

    print(f"削除対象 (プレフィックス {NAME_PREFIX!r}):", flush=True)
    for runtime in candidates:
        print(
            f"  - {runtime['agentRuntimeName']} ({runtime['agentRuntimeId']}) "
            f"status={runtime['status']}",
            flush=True,
        )

    if not apply_delete:
        print("\n--yes を付けずに実行したため削除しない。削除するには --yes を付けて再実行する。", flush=True)
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
            print(f"[skip] {name} はプレフィックスに一致しないためスキップする", flush=True)
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
            print(f"[{name}] 削除リクエスト失敗: {error.response['Error']['Code']}", flush=True)
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
