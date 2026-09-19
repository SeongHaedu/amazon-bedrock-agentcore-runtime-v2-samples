# agent-globalinit-probe/main.py
#
# 「起動時の処理はスナップショットに取り込まれる」という V2 の性質を体感するためのプローブ。
# グローバルスコープと遅延初期化のそれぞれに意図的な待ち時間 (既定 10 秒) を置き、
# どちらが invoke のレイテンシに残るかを 1 回の計測で同時に観測する。
#
# 期待される観測結果:
#   V2 : グローバル側の 10 秒は invoke に現れない (スナップショット準備で消費済み)。
#        遅延初期化の 10 秒はセッションの初回 invoke に必ず現れる。
#   V1 : ウォームプールに空きがなければ、両方の 10 秒が invoke に現れる。
#
# LLM は呼び出さない。モデル側のばらつきを排除するためである。
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone

GLOBAL_INIT_SECS = float(os.environ.get("GLOBAL_INIT_SECS", "10"))
LAZY_INIT_SECS = float(os.environ.get("LAZY_INIT_SECS", "10"))


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {exc}"


def _pid1_starttime():
    """/proc/1/stat の 22 番目 (0-indexed 21) が starttime (boot からの clock ticks) である。
    V2 では全セッションでこの値が一致する。同一のスナップショットから復元されているためである。"""
    stat = _read("/proc/1/stat")
    if stat.startswith("unavailable"):
        return stat
    return stat.split()[21]


# --- グローバルスコープ: V2 ではスナップショット準備時に 1 回だけ実行される領域 ---
_t0 = time.time()
print(f"[MODULE_START] {_t0}", flush=True)
time.sleep(GLOBAL_INIT_SECS)

# スナップショットに焼き込まれる値。invoke のレスポンスで返し、全セッションで同一かを確認する。
# wall_clock をここで取っているのは意図的である。V2 ではこの値がスナップショット作成時刻に
# 固定され、invoke 時点とのずれが時間とともに広がる。時刻・認証情報・乱数シード・確立済みの
# ネットワーク接続をグローバルスコープで保持してはならない理由がそのまま観測できる。
BAKED = {
    "uuid": str(uuid.uuid4()),
    "pid": os.getpid(),
    "wall_clock": datetime.now(timezone.utc).isoformat(),
    "rand": random.random(),
    "global_init_secs": round(time.time() - _t0, 3),
    "monotonic": round(time.monotonic(), 3),
    "pid1_starttime": _pid1_starttime(),
    "uptime": _read("/proc/uptime"),
}
print(f"[MODULE_END] {json.dumps(BAKED)}", flush=True)

from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402

app = BedrockAgentCoreApp()

# --- 遅延初期化: スナップショットには焼き込まれず、各セッションの初回 invoke で実行される領域 ---
_lazy = None


@app.entrypoint
def invoke(payload, context):
    started = time.time()
    global _lazy
    lazy_ran = _lazy is None
    if lazy_ran:
        time.sleep(LAZY_INIT_SECS)
        _lazy = {"ready_at": datetime.now(timezone.utc).isoformat()}

    session_id = getattr(context, "session_id", None)
    print(f"[ENTRYPOINT] session_id={session_id} lazy_ran={lazy_ran}", flush=True)
    return {
        "baked": BAKED,
        "now": datetime.now(timezone.utc).isoformat(),
        "now_monotonic": round(time.monotonic(), 3),
        "lazy_ran": lazy_ran,
        "lazy_secs": round(time.time() - started, 3),
        "lazy_ready_at": _lazy["ready_at"],
        "pid_now": os.getpid(),
        "pid1_starttime_now": _pid1_starttime(),
        "uptime_now": _read("/proc/uptime"),
        "session_id": session_id,
    }


if __name__ == "__main__":
    app.run()
