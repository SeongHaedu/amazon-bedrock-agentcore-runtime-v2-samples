# agent-basic/main.py
#
# platformVersion V1 / V2 の差を最小構成で観測するためのエージェント。
# LLM は呼び出さない。モデル側のばらつきを排除し、起動のセマンティクスだけを見るためである。
#
# 3 種類のマーカーを stdout に出す。CloudWatch Logs で V1 / V2 の差がそのまま読める。
#   [MODULE_START] / [MODULE_END] : モジュールスコープ (グローバルスコープ) の実行
#   [ENTRYPOINT]                  : リクエストハンドラの実行
#
# V1 では新しい実行環境ごとに MODULE_START / MODULE_END が出る。
# V2 では作成 / 更新時のスナップショット準備で 1 回だけ出て、invoke のログには出ない。
import json
import os
import time
import uuid
from datetime import datetime, timezone

MODULE_START = time.time()
print(f"[MODULE_START] {MODULE_START}", flush=True)

from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

app = BedrockAgentCoreApp()

# スナップショットに焼き込まれる値。V2 では復元された全インスタンスがこの同じ値を共有する。
# 復元ごとに再生成されないことを invoke のレスポンスで確認できる。
SNAPSHOT_IDENTITY = {
    "uuid": str(uuid.uuid4()),
    "pid": os.getpid(),
    "module_wall_clock": datetime.now(timezone.utc).isoformat(),
}

MODULE_END = time.time()
print(
    f"[MODULE_END] {MODULE_END} "
    f"elapsed_ms={(MODULE_END - MODULE_START) * 1000:.1f} "
    f"snapshot_identity={json.dumps(SNAPSHOT_IDENTITY)}",
    flush=True,
)


@app.entrypoint
def invoke(payload, context):
    print(
        f"[ENTRYPOINT] {time.time()} session_id={getattr(context, 'session_id', None)}",
        flush=True,
    )
    return {
        # V2 では全セッションでこの値が一致する。V1 ではセッションごとに異なる。
        "snapshot_identity": SNAPSHOT_IDENTITY,
        # リクエストごとに変わるべき値はハンドラ側で生成する。
        "request_uuid": str(uuid.uuid4()),
        "now": datetime.now(timezone.utc).isoformat(),
        "echo": payload,
        "session_id": getattr(context, "session_id", None),
    }


if __name__ == "__main__":
    app.run()
